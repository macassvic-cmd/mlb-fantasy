"""
Shared Discord delivery health tracking for Soccer DNS (2026-09-14,
coverage-depth + production-scheduling phase) - both soccer_alerts.py
(real-time alerts) and soccer_daily_digest.py (daily digest) post to
Discord independently; this is the one shared place that records
EVERY attempt from either of them, so "is Discord actually working"
is answerable without cross-referencing two separate data files.

Persisted to data/soccer_discord_health.json:
  last_attempt_at / last_attempt_source
  last_success_at / last_success_source
  last_failure_at / last_failure_source / last_error
Each field updates independently - a success never clears the last-
failure record (and vice versa), so "when did this last actually break"
stays answerable even after a subsequent success.
"""

import json
import os
import re
from datetime import datetime, timezone

HEALTH_PATH = os.path.join("data", "soccer_discord_health.json")

# 2026-09-15 security fix: a raised requests exception's own str(e) often
# embeds the full request URL - for a Discord webhook POST, that URL IS a
# bearer credential (anyone holding it can post to the channel). Nothing
# derived from an exception should ever be trusted as safe-to-persist by
# construction. This module is the one shared choke point every Discord-
# sending caller already routes error text through (see record_attempt),
# so redaction happens here once rather than being re-implemented (and
# potentially forgotten) at every call site.
_WEBHOOK_URL_RE = re.compile(r"https?://(?:[\w-]+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w-]+", re.I)
# Env vars whose NAME looks secret-shaped - any var matching this pattern
# has its actual, current VALUE redacted out of text wherever it appears,
# not just the Discord webhook case, since a Discord POST failure's
# exception text is not the only place a secret could accidentally leak
# into (e.g. a proxy/auth error mentioning X_BEARER_TOKEN's value).
_SECRET_ENV_NAME_RE = re.compile(r"(WEBHOOK|TOKEN|API_KEY|SECRET|PASSWORD)", re.I)
_MIN_SECRET_LEN = 8  # avoid redacting a short, common substring by coincidence


def redact(text):
    """Strips a Discord webhook URL, and the literal value of any
    currently-configured secret-shaped env var, out of `text`. Safe to
    call on anything before it's written to data/*.json or rendered into
    docs/ - a no-op on None/empty input, never raises on non-string
    surprises (returns the input unchanged rather than crashing a caller
    that was just trying to record an error)."""
    if not text or not isinstance(text, str):
        return text
    text = _WEBHOOK_URL_RE.sub("[REDACTED_WEBHOOK_URL]", text)
    for name, value in os.environ.items():
        if value and len(value) >= _MIN_SECRET_LEN and _SECRET_ENV_NAME_RE.search(name) and value in text:
            text = text.replace(value, f"[REDACTED_{name}]")
    return text


def _load():
    if os.path.exists(HEALTH_PATH):
        try:
            with open(HEALTH_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save(state):
    os.makedirs(os.path.dirname(HEALTH_PATH) or ".", exist_ok=True)
    with open(HEALTH_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def record_attempt(source, delivered, http_status=None, error=None, now=None):
    """source: a short label ("realtime_alert", "daily_digest", "test").
    Call this for EVERY Discord POST attempt, success or failure -
    never only on success (that's exactly what would make "last
    successful delivery" indistinguishable from "never checked").

    `error` is always passed through redact() before being persisted -
    a backstop (2026-09-15 security fix), not a substitute for callers
    building a sanitized message themselves: this file is the one place
    every caller's error text already flows through, so redaction here
    protects even a call site that forgets to sanitize its own message."""
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    state = _load()
    state["last_attempt_at"] = now_iso
    state["last_attempt_source"] = source
    state["last_attempt_delivered"] = bool(delivered)
    state["last_attempt_http_status"] = http_status
    if delivered:
        state["last_success_at"] = now_iso
        state["last_success_source"] = source
    else:
        state["last_failure_at"] = now_iso
        state["last_failure_source"] = source
        state["last_error"] = redact(error)
    _save(state)
    return state


def get_health():
    """Always returns a dict with every key present (None if never
    recorded) - a dashboard can render this directly without per-key
    existence checks."""
    state = _load()
    return {
        "last_attempt_at": state.get("last_attempt_at"),
        "last_attempt_source": state.get("last_attempt_source"),
        "last_attempt_delivered": state.get("last_attempt_delivered"),
        "last_success_at": state.get("last_success_at"),
        "last_success_source": state.get("last_success_source"),
        "last_failure_at": state.get("last_failure_at"),
        "last_failure_source": state.get("last_failure_source"),
        "last_error": state.get("last_error"),
    }
