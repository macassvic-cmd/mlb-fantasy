"""
OddsBlaze client for Stack Forge / the Play Card (2026-09-16).

Guardrails, all enforced here so callers can't forget them:
  * KEY: ODDSBLAZE_API_KEY from the environment (GitHub Actions secret, or
    .env locally via python-dotenv). Never printed, never logged, never
    written to data/ or docs/ - every saved URL has the key replaced with
    <redacted>, log lines carry params minus the key, and error text is
    passed through discord_health.redact() (which strips any secret-shaped
    env var's value) before being persisted anywhere.
  * BUDGET GUARD (BudgetGuard): rolling 60s windows under the documented
    caps (odds 30/min -> we use 25; SGP 10/min -> we use 8), minimum call
    spacing (odds 1.2s, SGP 6s - a burst of 8 odds calls in 3s drew 429s
    on 2026-09-16 despite being under the per-minute cap), and a per-day
    counter persisted to data/oddsblaze/budget.json so a whole day's runs
    (GitHub Actions + local) share one ceiling. `remaining(klass)` lets
    stack_forge price fewer variants instead of failing.
  * 429 / 5xx / timeouts: bounded retry with exponential backoff, honoring
    Retry-After (capped). 401/403 -> AccessBlocked; 4xx else -> InvalidResponse.
  * RAW RESPONSES: every response saved to data/oddsblaze/raw/<endpoint>/
    <utc ts>.json as {"meta": {...}, "body": ...} (gitignored - multi-MB).
  * OFFLINE REPLAY: OddsBlazeClient(offline=True) serves the newest saved
    raw response with the same endpoint + params signature instead of
    calling the network - the "one-line swap" for when the key is missing
    or expired; callers see status 200 with meta["replayed"] = True.

Endpoints (docs.oddsblaze.com/endpoints/*, verified live 2026-09-16):
  odds        GET  https://odds.oddsblaze.com/?key&sportsbook&league[&market&event&main&live&price]
  sgp         POST https://{sportsbook}.sgp.oddsblaze.com/?key[&price]   body: ["<sgp token>", ...]
  schedule    GET  https://schedule.oddsblaze.com/?key&league[&date]
  sportsbooks GET  https://sportsbooks.oddsblaze.com/?key
"""

import glob
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass

try:
    from discord_health import redact as _redact_secrets
except Exception:  # pragma: no cover
    def _redact_secrets(text):
        return text

logger = logging.getLogger("oddsblaze_client")

DATA_DIR = os.path.join("data", "oddsblaze")
RAW_DIR = os.path.join(DATA_DIR, "raw")
BUDGET_PATH = os.path.join(DATA_DIR, "budget.json")

ODDS_URL = "https://odds.oddsblaze.com/"
SCHEDULE_URL = "https://schedule.oddsblaze.com/"
SPORTSBOOKS_URL = "https://sportsbooks.oddsblaze.com/"
SGP_URL_TEMPLATE = "https://{sportsbook}.sgp.oddsblaze.com/"

# Documented caps: odds 30/min, SGP 10/min. Budgets sit under them.
PER_MINUTE = {"odds": 25, "sgp": 8}
MIN_SPACING_SECONDS = {"odds": 1.2, "sgp": 6.0}
DAILY_CAP_DEFAULT = {"odds": 3000, "sgp": 1500}   # override with ODDSBLAZE_DAILY_CAP_ODDS / _SGP
WINDOW_SECONDS = 60.0
MAX_ATTEMPTS = 4
BACKOFF_BASE = 2.0
BACKOFF_MAX = 60.0
RETRYABLE = {408, 425, 429, 500, 502, 503, 504}
BLOCKED = {401, 403}


class OddsBlazeError(Exception):
    pass


class AccessBlocked(OddsBlazeError):
    """401/403 - expired trial key, wrong key, or blocked. Not retried."""


class InvalidResponse(OddsBlazeError):
    pass


class BudgetExhausted(OddsBlazeError):
    """The per-day cap for this call class is spent."""


class NoKey(OddsBlazeError):
    pass


def redact_key(text: str, key: Optional[str]) -> str:
    if not text:
        return text
    if key:
        text = text.replace(key, "<redacted>")
    text = re.sub(r"key=[^&\s\"']*", "key=<redacted>", text)
    return _redact_secrets(text)


def load_api_key() -> Optional[str]:
    return (os.environ.get("ODDSBLAZE_API_KEY") or "").strip() or None


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class BudgetGuard:
    """Per-class rolling-minute window + minimum spacing + persisted daily
    counter. `wait(klass)` blocks until a slot is free (raises
    BudgetExhausted if the day's cap is spent); `remaining(klass)` is the
    number of calls still allowed today, so a planner can trim work."""

    def __init__(self, per_minute=None, spacing=None, daily_cap=None, path=BUDGET_PATH, sleep_fn=time.sleep, clock=time.monotonic):
        self.per_minute = dict(per_minute or PER_MINUTE)
        self.spacing = dict(spacing or MIN_SPACING_SECONDS)
        self.daily_cap = dict(daily_cap or DAILY_CAP_DEFAULT)
        for klass in list(self.daily_cap):
            env = os.environ.get(f"ODDSBLAZE_DAILY_CAP_{klass.upper()}")
            if env and env.isdigit():
                self.daily_cap[klass] = int(env)
        self.path = path
        self.sleep_fn = sleep_fn
        self.clock = clock
        self.windows = {k: deque() for k in self.per_minute}
        self.lock = threading.Lock()
        self.state = self._load()

    def _load(self):
        state = {"date": _today(), "counts": {}, "last_429_at": None}
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    saved = json.load(f)
                if saved.get("date") == state["date"]:
                    state = saved
            except Exception:
                pass
        return state

    def _save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.path)

    def _roll_date(self):
        if self.state.get("date") != _today():
            self.state = {"date": _today(), "counts": {}, "last_429_at": None}

    def used_today(self, klass) -> int:
        self._roll_date()
        return int(self.state.get("counts", {}).get(klass, 0))

    def remaining(self, klass) -> int:
        return max(0, self.daily_cap.get(klass, 0) - self.used_today(klass))

    def wait(self, klass):
        if self.remaining(klass) <= 0:
            raise BudgetExhausted(f"daily {klass} cap {self.daily_cap.get(klass)} reached")
        window = self.windows[klass]
        while True:
            with self.lock:
                now = self.clock()
                while window and now - window[0] > WINDOW_SECONDS:
                    window.popleft()
                since_last = now - window[-1] if window else None
                spacing = self.spacing.get(klass, 0.0)
                if len(window) < self.per_minute[klass] and (since_last is None or since_last >= spacing):
                    window.append(now)
                    self.state.setdefault("counts", {})[klass] = self.used_today(klass) + 1
                    self._save()
                    return
                if since_last is not None and since_last < spacing:
                    sleep_for = spacing - since_last
                else:
                    sleep_for = WINDOW_SECONDS - (now - window[0]) + 0.05
            self.sleep_fn(max(0.05, sleep_for))

    def note_429(self):
        self.state["last_429_at"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def summary(self):
        return {k: {"used_today": self.used_today(k), "daily_cap": self.daily_cap.get(k), "per_minute": self.per_minute.get(k)} for k in self.per_minute}


def _sig(endpoint, params, body):
    blob = json.dumps({"e": endpoint, "p": params, "b": body}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


class OddsBlazeClient:
    def __init__(self, key: Optional[str] = None, timeout: float = 30.0, guard: Optional[BudgetGuard] = None, session=None,
                 raw_dir: str = RAW_DIR, offline: bool = False, sleep_fn=time.sleep, rng=None):
        self.key = key if key is not None else load_api_key()
        self.offline = offline
        if not self.key and not offline:
            raise NoKey("ODDSBLAZE_API_KEY is not set - run with offline=True to replay saved responses")
        self.timeout = timeout
        self.guard = guard or BudgetGuard()
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "mlb-fantasy/stack-forge (read-only)"
        self.raw_dir = raw_dir
        self.sleep_fn = sleep_fn
        self.rng = rng or (lambda: 0.5)
        self.calls = {"odds": 0, "sgp": 0, "replayed": 0}

    # ---- endpoints ----

    def sportsbooks(self):
        return self._request("GET", "sportsbooks", SPORTSBOOKS_URL, {}, "odds")

    def schedule(self, league, **params):
        return self._request("GET", f"schedule_{league}", SCHEDULE_URL, {"league": league, **params}, "odds")

    def odds(self, sportsbook, league, tag=None, **params):
        params = {"sportsbook": sportsbook, "league": league, **{k: v for k, v in params.items() if v is not None}}
        return self._request("GET", tag or f"odds_{sportsbook}_{league}", ODDS_URL, params, "odds")

    def sgp(self, sportsbook, tokens, price="decimal", tag=None):
        """tokens: list of sgp token strings (verified: books accept a plain
        JSON array of strings; the object form fails)."""
        return self._request("POST", tag or f"sgp_{sportsbook}", SGP_URL_TEMPLATE.format(sportsbook=sportsbook), {"price": price}, "sgp", body=list(tokens))

    # ---- core ----

    def _request(self, method, endpoint, url, params, klass, body=None):
        if self.offline:
            return self._replay(endpoint, params, body)
        self.guard.wait(klass)
        sent = dict(params, key=self.key)
        last_err = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = datetime.now(timezone.utc)
            t0 = time.monotonic()
            try:
                resp = self.session.request(method, url, params=sent, json=body, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = f"{type(e).__name__}"
                self._backoff(attempt, None)
                continue
            elapsed = time.monotonic() - t0
            self.calls[klass] += 1
            parsed = self._save(endpoint, resp, params, body, started, elapsed)
            logger.info("%s %s params=%s status=%s bytes=%d %.2fs", method, endpoint, json.dumps(params, sort_keys=True), resp.status_code, len(resp.content), elapsed)
            if resp.status_code in BLOCKED:
                raise AccessBlocked(f"HTTP {resp.status_code} from {endpoint} - key rejected/expired or access blocked")
            if resp.status_code in RETRYABLE:
                if resp.status_code == 429:
                    self.guard.note_429()
                if attempt < MAX_ATTEMPTS:
                    self._backoff(attempt, resp.headers.get("Retry-After"))
                    self.guard.wait(klass)
                    continue
                raise InvalidResponse(f"HTTP {resp.status_code} from {endpoint} after {MAX_ATTEMPTS} attempts")
            return resp.status_code, parsed
        raise InvalidResponse(f"{endpoint}: gave up after {MAX_ATTEMPTS} attempts ({last_err})")

    def _backoff(self, attempt, retry_after):
        delay = None
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = None
        if delay is None:
            delay = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_MAX) * (0.5 + 0.5 * self.rng())
        self.sleep_fn(min(delay, BACKOFF_MAX))

    def _folder(self, endpoint):
        return os.path.join(self.raw_dir, re.sub(r"[^a-z0-9_\-]", "_", endpoint.lower()))

    def _save(self, endpoint, resp, params, body, started, elapsed):
        try:
            parsed = resp.json()
        except ValueError:
            parsed = redact_key(resp.text, self.key)
        meta = {
            "endpoint": endpoint, "signature": _sig(endpoint, params, body), "requested_at_utc": started.isoformat(),
            "elapsed_seconds": round(elapsed, 3), "method": resp.request.method if resp.request else None,
            "url": redact_key(resp.url or "", self.key), "params": params, "request_body": body,
            "status": resp.status_code, "bytes": len(resp.content),
            "response_headers": {k: v for k, v in resp.headers.items() if k.lower() in ("date", "content-type", "cache-control", "etag", "retry-after")},
        }
        folder = self._folder(endpoint)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, started.strftime("%Y%m%dT%H%M%S_%fZ") + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "body": parsed}, f, ensure_ascii=False)
        return parsed

    def _replay(self, endpoint, params, body):
        sig = _sig(endpoint, params, body)
        newest = None
        for f in sorted(glob.glob(os.path.join(self._folder(endpoint), "*.json"))):
            try:
                with open(f, encoding="utf-8") as fh:
                    j = json.load(fh)
            except Exception:
                continue
            if j.get("meta", {}).get("signature") == sig and j["meta"].get("status") == 200:
                newest = j
        if newest is None:
            raise InvalidResponse(f"offline replay: no saved 200 response for {endpoint} {json.dumps(params, sort_keys=True)}")
        self.calls["replayed"] += 1
        parsed = newest["body"]
        if isinstance(parsed, dict):
            parsed = dict(parsed, _replayed_from=newest["meta"]["requested_at_utc"])
        return 200, parsed
