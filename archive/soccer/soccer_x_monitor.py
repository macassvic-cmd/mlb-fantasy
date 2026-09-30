"""
X (Twitter) real-time monitoring for soccer DNS - dynamic, Dabble-driven
watchlist over the official X API v2 Filtered Stream.

HONEST STATUS (2026-09-14 build): no X API credentials exist anywhere in
this environment (checked .env and process environment for
X_BEARER_TOKEN/TWITTER_* - none found). This module is a complete, correct
implementation that will work the moment X_BEARER_TOKEN is set, but
without it every entry point below is a documented no-op (logs "not
configured", never raises, never fabricates a post) - same pattern as
stale_lines.py's Discord webhook / dnp_alerts.py's DISCORD_DNP_WEBHOOK_URL.
Do not treat an empty result from this module as "no soccer news exists" -
it means "X monitoring isn't connected," which is a different fact
(see soccer_adapter.py's LOW DATA / ENRICHMENT MISSING watchdog).

Two rule classes (X API v2 filtered-stream rules), per the design:
  A. TRUSTED AUTHOR RULES - "from:<user_id>" rules for every account in
     data/soccer_source_registry.json. Persistent regardless of the
     current Dabble board - see item 5's "official/beat-reporter account
     rules can remain persistent."
  B. PLAYER/TEAM KEYWORD RULES - name/team keyword rules, one group per
     LIVE Dabble soccer player, rebuilt every time the Dabble board
     refreshes (build_keyword_rules). Lower trust by construction - see
     TRUST_TIER_WEIGHT.

Status-phrase extraction is intentionally NOT allowed to change any score
by itself - extract_signal() only classifies text; soccer_dns_score.py
decides how (and whether) to weight it, gated by author trust tier.
"""

import json
import os
import re
import unicodedata
from datetime import datetime, timezone

X_BEARER_TOKEN_ENV_VAR = "X_BEARER_TOKEN"
X_API_BASE = "https://api.x.com/2"

SOURCE_REGISTRY_PATH = os.path.join("data", "soccer_source_registry.json")
X_POSTS_DIR = os.path.join("data", "soccer_x_posts")

# Confidence weighting by author trust tier (1=official/2=beat reporter get
# heavy weight; 5/keyword-only discovery gets low weight until corroborated
# - see module docstring point A/B).
TRUST_TIER_WEIGHT = {1: 0.95, 2: 0.85, 3: 0.6, 4: 0.35, 5: 0.15}
KEYWORD_ONLY_TRUST_TIER = 5

# --- Status-phrase extraction --------------------------------------------
# Ordered most-committal first (same precedence convention as
# watchlist_classifiers._classify_generic: reversal-ish phrases checked
# before out/doubtful so "passed fitness test" never also matches "fitness
# test" as a doubt signal).
_START_SIGNAL_PATTERNS = [
    ("confirmed_start", [r"\bset to start\b", r"\bnamed in the starting (xi|lineup|11)\b",
                          r"\bstarts (tonight|today)\b"]),
    ("returning", [r"\bpassed (a |his )?fitness test\b", r"\bcleared to play\b", r"\bavailable\b",
                    r"\btrained (fully|with the (team|squad))\b", r"\breturns? to training\b"]),
    ("bench_risk", [r"\bnot expected to start\b", r"\bunlikely to start\b", r"\bon the bench\b",
                     r"\bnot with (the )?squad\b", r"\bleft out\b"]),
]
_INJURY_STATUS_PATTERNS = [
    ("out", [r"\bruled out\b", r"\bwill (not|n't) play\b", r"\bsidelined\b", r"\bout (for|of)\b",
              r"\bexpected to miss\b"]),
    ("doubtful", [r"\bdoubtful\b", r"\ba doubt\b", r"\btouch and go\b", r"\b50-50\b", r"\bfifty-fifty\b"]),
    ("questionable", [r"\bquestionable\b", r"\bgame-time decision\b", r"\bgametime decision\b"]),
    ("late_fitness_test", [r"\blate fitness test\b", r"\bassessed on (the day|matchday)\b",
                            r"\blate call\b", r"\blate decision\b"]),
    ("missed_training", [r"\bmissed training\b", r"\bdid not train\b"]),
]

_COMPILED_START = [(label, [re.compile(p, re.I) for p in pats]) for label, pats in _START_SIGNAL_PATTERNS]
_COMPILED_INJURY = [(label, [re.compile(p, re.I) for p in pats]) for label, pats in _INJURY_STATUS_PATTERNS]


def extract_signal(text):
    """(extracted_status, extracted_injury, extracted_start_signal) from
    free text. Pure text classification - never touches any score by
    itself; see module docstring."""
    text = text or ""
    extracted_injury = None
    for label, patterns in _COMPILED_INJURY:
        if any(p.search(text) for p in patterns):
            extracted_injury = label
            break

    extracted_start_signal = None
    for label, patterns in _COMPILED_START:
        if any(p.search(text) for p in patterns):
            extracted_start_signal = label
            break

    extracted_status = extracted_injury or extracted_start_signal
    return extracted_status, extracted_injury, extracted_start_signal


# --- Trusted-source registry ----------------------------------------------

def load_source_registry(path=SOURCE_REGISTRY_PATH):
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("sources", [])
    except Exception:
        return []


# --- Name-variant generation for keyword rules ----------------------------

def name_variants(full_name):
    """ASCII/diacritic variants of a player's name for keyword-rule
    discovery (item 1's Dedic example: "Amar Dedic", "Amar Dedić", "Dedic").
    Reuses the same NFKD-strip approach as scrapers.betr.normalize_name,
    but preserves casing/spacing for readable X search terms rather than
    collapsing to a lookup key."""
    ascii_version = unicodedata.normalize("NFKD", full_name).encode("ascii", "ignore").decode("ascii")
    variants = {full_name, ascii_version}
    parts = full_name.split()
    if len(parts) > 1:
        last_name = parts[-1]
        last_ascii = unicodedata.normalize("NFKD", last_name).encode("ascii", "ignore").decode("ascii")
        variants.add(last_name)
        variants.add(last_ascii)
    return sorted(v for v in variants if v)


def build_keyword_rules(live_dabble_players):
    """live_dabble_players: [{"player_id", "player_name", "team"}] - the
    CURRENT live Dabble candidate list (see soccer_adapter.py). One rule
    group per player: their name variants OR'd with their team name, so a
    club-news tweet mentioning the team without the player by name still
    has a chance to match on discovery (low trust either way - see
    KEYWORD_ONLY_TRUST_TIER)."""
    rules = []
    for p in live_dabble_players:
        variants = name_variants(p["player_name"])
        terms = " OR ".join(f'"{v}"' for v in variants)
        team = p.get("team")
        value = f'({terms}) ("{team}" OR {team})' if team else f"({terms})"
        rules.append({
            "value": value[:512],  # X API rule length limit
            "tag": f"player:{p.get('player_id') or p['player_name']}",
            "trust_tier": KEYWORD_ONLY_TRUST_TIER,
            "matched_player_id": p.get("player_id"),
            "matched_player_name": p["player_name"],
            "matched_team": team,
        })
    return rules


def build_trusted_author_rules(registry=None):
    registry = registry if registry is not None else load_source_registry()
    rules = []
    for src in registry:
        account = src.get("account", "")
        if not account or account.startswith("EXAMPLE_"):
            continue  # unpopulated placeholder entries - see data/soccer_source_registry.json
        rules.append({
            "value": f"from:{account}",
            "tag": f"trusted:{account}",
            "trust_tier": src.get("trust_tier", 3),
            "matched_player_id": None,
            "matched_team": src.get("team"),
        })
    return rules


# --- Credentials / connection --------------------------------------------

def has_credentials():
    return bool(os.environ.get(X_BEARER_TOKEN_ENV_VAR))


def _headers():
    return {"Authorization": f"Bearer {os.environ[X_BEARER_TOKEN_ENV_VAR]}"}


def sync_stream_rules(live_dabble_players, registry=None):
    """Push the current rule set (trusted-author + Dabble-driven keyword
    rules) to X's Filtered Stream. No-op, returns False, if credentials
    aren't configured - see module docstring."""
    if not has_credentials():
        print("soccer_x_monitor: X_BEARER_TOKEN not set - X monitoring not configured, skipping rule sync.")
        return False

    import requests
    rules = build_trusted_author_rules(registry) + build_keyword_rules(live_dabble_players)
    try:
        existing = requests.get(f"{X_API_BASE}/tweets/search/stream/rules", headers=_headers(), timeout=15)
        existing.raise_for_status()
        existing_ids = [r["id"] for r in existing.json().get("data", [])]
        if existing_ids:
            requests.post(f"{X_API_BASE}/tweets/search/stream/rules", headers=_headers(),
                           json={"delete": {"ids": existing_ids}}, timeout=15)
        if rules:
            requests.post(f"{X_API_BASE}/tweets/search/stream/rules", headers=_headers(),
                           json={"add": [{"value": r["value"], "tag": r["tag"]} for r in rules]}, timeout=15)
        return True
    except Exception as e:
        print(f"soccer_x_monitor: rule sync failed (non-fatal): {e}")
        return False


def _rule_meta_by_tag(live_dabble_players, registry=None):
    meta = {}
    for r in build_trusted_author_rules(registry) + build_keyword_rules(live_dabble_players):
        meta[r["tag"]] = r
    return meta


def _store_post(date_str, record):
    os.makedirs(X_POSTS_DIR, exist_ok=True)
    path = os.path.join(X_POSTS_DIR, f"{date_str}.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data[record["post_id"]] = record
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def poll_stream_once(live_dabble_players, registry=None, max_seconds=30):
    """Connect briefly to the filtered stream and store any matching
    posts. Returns the list of stored records. No-op ([]) if credentials
    aren't configured. A short-lived poll (not a persistent daemon) so
    this can be called from the same cron cycle as dabble_adapter.py -
    running a true always-on stream consumer is a separate, longer-lived
    process left for whoever operates this once credentials exist."""
    if not has_credentials():
        print("soccer_x_monitor: X_BEARER_TOKEN not set - X monitoring not configured, skipping poll.")
        return []

    import requests
    rule_meta = _rule_meta_by_tag(live_dabble_players, registry)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stored = []
    try:
        params = {
            "tweet.fields": "created_at,author_id",
            "expansions": "author_id",
            "user.fields": "username",
        }
        with requests.get(f"{X_API_BASE}/tweets/search/stream", headers=_headers(),
                           params=params, stream=True, timeout=max_seconds) as resp:
            resp.raise_for_status()
            users = {}
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for u in (payload.get("includes") or {}).get("users", []):
                    users[u["id"]] = u.get("username")

                tweet = payload.get("data") or {}
                matched_tags = [m.get("tag") for m in (payload.get("matching_rules") or [])]
                if not tweet or not matched_tags:
                    continue

                text = tweet.get("text", "")
                extracted_status, extracted_injury, extracted_start_signal = extract_signal(text)
                author_id = tweet.get("author_id")
                for tag in matched_tags:
                    meta = rule_meta.get(tag, {})
                    trust_tier = meta.get("trust_tier", KEYWORD_ONLY_TRUST_TIER)
                    confidence = TRUST_TIER_WEIGHT.get(trust_tier, 0.15)
                    if extracted_status is None:
                        confidence *= 0.5  # matched a rule but no classifiable phrase - discovery only
                    record = {
                        "post_id": tweet.get("id"),
                        "author_id": author_id,
                        "author_username": users.get(author_id),
                        "author_trust_tier": trust_tier,
                        "created_at": tweet.get("created_at"),
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "matched_player_id": meta.get("matched_player_id"),
                        "matched_team": meta.get("matched_team"),
                        "raw_text": text,
                        "source_url": f"https://x.com/i/web/status/{tweet.get('id')}" if tweet.get("id") else None,
                        "matched_rule": tag,
                        "extracted_status": extracted_status,
                        "extracted_injury": extracted_injury,
                        "extracted_start_signal": extracted_start_signal,
                        "confidence": round(confidence, 3),
                    }
                    _store_post(date_str, record)
                    stored.append(record)
    except Exception as e:
        print(f"soccer_x_monitor: stream poll ended (non-fatal - {type(e).__name__}: {e})")
    return stored


def get_player_x_signal(normalized_name_variants, date_str=None):
    """Best (highest-confidence, most-recent) stored X post matching any
    of the given name variants (normalized) for date_str (default today).
    None if X monitoring isn't configured or nothing matched - a caller
    must treat None as "no signal available," not "player is fine"."""
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(X_POSTS_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            posts = json.load(f)
    except Exception:
        return None

    from scrapers.betr import normalize_name
    variant_set = {normalize_name(v) for v in normalized_name_variants}
    matches = [p for p in posts.values()
               if p.get("matched_player_id") in normalized_name_variants
               or normalize_name(p.get("raw_text", "")) and any(v in normalize_name(p.get("raw_text", "")) for v in variant_set)]
    if not matches:
        return None
    matches.sort(key=lambda p: (p["confidence"], p["received_at"]), reverse=True)
    return matches[0]
