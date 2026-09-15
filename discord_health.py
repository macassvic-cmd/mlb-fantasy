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
from datetime import datetime, timezone

HEALTH_PATH = os.path.join("data", "soccer_discord_health.json")


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
    successful delivery" indistinguishable from "never checked")."""
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
        state["last_error"] = error
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
