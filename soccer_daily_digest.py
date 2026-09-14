"""
Daily Soccer DNS Discord digest - one scheduled, once-a-day OVERVIEW of
the ENTIRE live Dabble soccer board, deliberately separate from soccer_
alerts.py's real-time threshold-crossing/news-signal alerts (2026-09-14
coverage-audit follow-up request).

WHY A SEPARATE MODULE (not folded into soccer_alerts.py): the two have
different dedup semantics that must never interact. soccer_alerts.py
dedupes by (player, fixture, threshold/signal) so the SAME alert never
fires twice - a player appearing in this morning's digest must not
suppress a later real-time alert if he WORSENS, and a real-time alert
firing must not suppress tomorrow's digest line for the same player.
This module's only dedup question is "did TODAY's digest already get
delivered" - see run_daily_digest().

SCHEDULE: intended to run once daily at 7:00 AM Pacific Time, BEFORE the
day's main soccer slate - see DAILY_DIGEST_SCHEDULE below and run_
soccer_daily_digest.bat / setup_soccer_daily_digest_task.ps1. This module
and its scheduler wrapper treat "7:00 AM" as the HOST MACHINE'S OWN LOCAL
WALL CLOCK - there is no internal timezone conversion. If the host
machine's local timezone is not Pacific, whoever configures the
scheduled task must set the trigger time accordingly (see that .ps1's
own comment) for this to actually fire at 7am PT in wall-clock terms.
DAILY_DIGEST_SCHEDULE is a real, reusable list of named slots (adding a
second "midday_update" entry and setting its own scheduled task later
is enough to enable it) - only "morning_board" is wired to an actual
Task Scheduler entry right now, per the request to keep the second slot
dormant-but-ready.

FLOW (do not reorder - see run_daily_digest): refresh the live Dabble
board -> enrich every source -> score DNS -> persist the live snapshot
-> build the digest -> send to Discord. This module NEVER reads
yesterday's (or an hour-old) already-persisted snapshot and calls that
"today's digest" - every run re-scores the board fresh, specifically so
a digest can never silently go out describing a stale board. If the
fresh scoring pass itself fails or the board data looks stale (see
_board_freshness), Discord gets a system-warning message instead of a
normal digest - see _stale_warning_message.

PERSISTENCE (data/soccer_daily_digest/{date}.json): discord_attempted
and discord_delivered are tracked as SEPARATE facts, same discipline as
every other webhook integration in this codebase (dnp_alerts.py/
soccer_alerts.py) - a digest is never marked "sent" just because it was
generated; "attempted but Discord was down" stays retryable via --force.
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import requests

DIGEST_DIR = os.path.join("data", "soccer_daily_digest")
WEBHOOK_ENV_VAR = "DISCORD_SOCCER_DNS_WEBHOOK_URL"  # same channel/env var as soccer_alerts.py's real-time alerts

WATCH_THRESHOLD = 70   # matches soccer_alerts.py's WATCH tier floor
HIGH_THRESHOLD = 85    # matches soccer_alerts.py's HIGH PRIORITY tier floor
MIN_CANDIDATES_SHOWN = 15

# Board data older than this is treated as a failed refresh (Discord gets
# the stale-data warning instead of a normal digest) - generous relative
# to how often the live Dabble board actually updates (see soccer_
# adapter.py/dns_watch.py), erring toward "still show something" over
# "false-alarm every quiet stretch."
STALE_BOARD_MAX_AGE_HOURS = 6

# Reusable, not-yet-enabled second slot (item "optional second refresh") -
# enabling it later means adding its own real Task Scheduler entry, NOT
# a code change here; this list is what a human/ops script reads to know
# what SHOULD be scheduled, not a live in-process scheduler.
DAILY_DIGEST_SCHEDULE = [
    {"name": "morning_board", "time_local": "07:00", "enabled": True,
     "description": "Full Soccer DNS board overview before the day's main slate."},
    {"name": "midday_update", "time_local": "11:00", "enabled": False,
     "description": "Optional second refresh - NOT enabled; see module docstring."},
]


def _webhook_url():
    return os.environ.get(WEBHOOK_ENV_VAR)


def _digest_path(date_str):
    return os.path.join(DIGEST_DIR, f"{date_str}.json")


def _load_digest_record(date_str):
    path = _digest_path(date_str)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _save_digest_record(date_str, record):
    os.makedirs(DIGEST_DIR, exist_ok=True)
    with open(_digest_path(date_str), "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)


def _new_record(date_str):
    return {
        "digest_date": date_str, "generated_at": None, "candidate_count": None,
        "watch_count": None, "high_count": None, "discord_attempted": False,
        "discord_delivered": False, "discord_http_status": None, "discord_error": None,
    }


# ---------------------------------------------------------------------------
# Board freshness - "refresh Dabble board -> ... -> Discord"; a failed or
# stale refresh must produce a system warning, never a digest built off
# old data (see module docstring's FLOW section).
# ---------------------------------------------------------------------------

def _board_freshness(source):
    """(is_fresh: bool, age_hours: float_or_None, generated_at: str_or_None) -
    reads the board file's OWN generated_at timestamp (stamped by whatever
    produced it - see soccer_adapter.load_dabble_soccer_props) rather than
    the file's mtime, so a copied/re-saved-but-not-actually-refreshed file
    is correctly still judged stale."""
    if not os.path.exists(source):
        return False, None, None
    try:
        with open(source, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False, None, None
    generated_at = data.get("generated_at") if isinstance(data, dict) else None
    if not generated_at:
        return False, None, None
    try:
        gen_dt = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False, None, generated_at
    age_hours = (datetime.now(timezone.utc) - gen_dt).total_seconds() / 3600
    return age_hours <= STALE_BOARD_MAX_AGE_HOURS, round(age_hours, 1), generated_at


# ---------------------------------------------------------------------------
# Digest content
# ---------------------------------------------------------------------------

def select_digest_candidates(candidates):
    """(shown, watch_count, high_count) - sorted by combined_priority DESC,
    at least MIN_CANDIDATES_SHOWN plus every DNS>=WATCH_THRESHOLD candidate
    even if that makes the list longer (never truncates a real Watch+ risk
    just to hit a round number)."""
    ranked = sorted(candidates, key=lambda c: c.get("combined_priority") or 0, reverse=True)
    watch_count = sum(1 for c in ranked if c["dns_score"] >= WATCH_THRESHOLD)
    high_count = sum(1 for c in ranked if c["dns_score"] >= HIGH_THRESHOLD)

    cutoff = max(MIN_CANDIDATES_SHOWN, watch_count)
    shown = ranked[:cutoff]
    return shown, watch_count, high_count


def _game_time_pt(event_date):
    try:
        from report import game_time_pt
        return game_time_pt(event_date)
    except Exception:
        return event_date or "time TBD"


def _candidate_line(idx, c):
    dabble_status = c.get("dabble_status_normalized") or c.get("dabble_status_raw") or "n/a"
    rw = c.get("rotowire_page_tag") or c.get("rotowire_status_raw") or "n/a"
    rw_injury = f" - {c['rotowire_injury']}" if c.get("rotowire_injury") else ""
    injury = (c.get("transfermarkt_injury") or {}).get("reason")
    news = injury or (c.get("rotowire_injury") if c.get("has_news") else None) or "none"
    xi = c.get("prediction_consensus", "unknown")
    xi_count = f" {len(c.get('predicted_start_sources', []))}/{len(c.get('predicted_xi_sources', []))}" \
        if c.get("predicted_xi_sources") else ""
    reasons = "; ".join(c.get("top_reasons", [])[:3]) or "no itemized reasons"

    return (
        f"**{idx}. {c['player_name']} — {c['team']}**\n"
        f"DNS {c['dns_score']} | Conf {c['confidence_score']} | Urg {c['urgency_score']} | "
        f"Pri {c.get('combined_priority')}\n"
        f"{c.get('league_code') or c.get('league_raw') or '?'} — vs {c.get('matchup') or '?'} — "
        f"{_game_time_pt(c.get('event_date'))}\n"
        f"Dabble: 🟢 LIVE ({dabble_status}) | RotoWire: {rw}{rw_injury} | "
        f"Predicted XI: {xi}{xi_count}\n"
        f"Signal: {news}\n"
        f"Reason: {reasons}"
    )


def build_digest_embeds(shown, watch_count, high_count, total_live, generated_at_local_label):
    """Discord embeds for the full digest - chunked to stay under the
    4096-char-per-embed-description limit (same discipline as soccer_
    dns.py's build_digest_embeds for the MLB-style injury digest)."""
    header = (f"🟢 {total_live} Dabble players live\n"
              f"🎯 {watch_count} WATCH (70+)\n"
              f"🚨 {high_count} HIGH (85+)")
    lines = [_candidate_line(i, c) for i, c in enumerate(shown, 1)]

    embeds = []
    current, current_len = [], 0
    limit = 3800
    for line in lines:
        add_len = len(line) + 2
        if current and current_len + add_len > limit:
            embeds.append({"title": "⚽ SOCCER DNS — DAILY BOARD (cont.)" if embeds else "⚽ SOCCER DNS — DAILY BOARD",
                            "description": ("" if embeds else header + "\n\n") + "\n\n".join(current),
                            "color": 0x3498DB})
            current, current_len = [], 0
        current.append(line)
        current_len += add_len
    if current or not embeds:
        embeds.append({"title": "⚽ SOCCER DNS — DAILY BOARD (cont.)" if embeds else "⚽ SOCCER DNS — DAILY BOARD",
                        "description": ("" if embeds else header + "\n\n") + "\n\n".join(current),
                        "color": 0x3498DB})
    embeds[0]["footer"] = {"text": f"Updated: {generated_at_local_label}"}
    return embeds


def _zero_candidate_message():
    return "✅ Soccer DNS Daily Scan Complete — no qualifying candidates"


def _stale_warning_message(age_hours, generated_at, error=None):
    age_text = f"{age_hours}h old" if age_hours is not None else "age unknown"
    reason = f" ({error})" if error else ""
    return (f"🚨 Soccer DNS daily scan failed / data stale{reason}\n"
            f"Most recent successful board: {generated_at or 'never'} ({age_text}).")


# ---------------------------------------------------------------------------
# Discord delivery
# ---------------------------------------------------------------------------

def _post_discord(content=None, embeds=None):
    """(attempted: bool, delivered: bool, http_status: int_or_None,
    error: str_or_None). attempted is False only when no webhook is
    configured at all - a documented no-op, never conflated with a
    failed delivery attempt (see module docstring)."""
    webhook_url = _webhook_url()
    if not webhook_url:
        return False, False, None, "DISCORD_SOCCER_DNS_WEBHOOK_URL not set - digest not sent (documented no-op)."
    payload = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        delivered = 200 <= resp.status_code < 300
        return True, delivered, resp.status_code, (None if delivered else resp.text[:300])
    except Exception as e:
        return True, False, None, f"{type(e).__name__}: {e}"


def send_test_message():
    """python soccer_dns.py --test-discord - manual health-check send,
    separate from a real digest/alert, for confirming the webhook itself
    works end to end."""
    attempted, delivered, status, error = _post_discord(
        content="✅ Soccer DNS Discord test message - webhook is reachable and delivering.")
    print(f"discord_attempted={attempted} discord_delivered={delivered} "
          f"http_status={status} error={error}")
    return {"discord_attempted": attempted, "discord_delivered": delivered,
            "discord_http_status": status, "discord_error": error}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_daily_digest(force=False, source=None, date_str=None, now=None):
    """The full flow: refresh -> enrich -> score -> persist -> digest ->
    Discord. Idempotent per calendar day unless force=True - a second
    invocation the same day that already delivered successfully is a
    no-op (returns the existing record) rather than double-posting."""
    import soccer_adapter

    now = now or datetime.now(timezone.utc)
    date_str = date_str or now.strftime("%Y-%m-%d")
    source = source or soccer_adapter.DEFAULT_BOARD_PATH

    existing = _load_digest_record(date_str)
    if existing and existing.get("discord_delivered") and not force:
        print(f"soccer_daily_digest: {date_str} already delivered at generation time "
              f"{existing.get('generated_at')} - use --force to resend.")
        return existing

    record = _new_record(date_str)
    record["generated_at"] = now.isoformat()

    fresh, age_hours, board_generated_at = _board_freshness(source)
    if not fresh:
        attempted, delivered, status, error = _post_discord(
            content=_stale_warning_message(age_hours, board_generated_at))
        record.update(discord_attempted=attempted, discord_delivered=delivered,
                       discord_http_status=status, discord_error=error, candidate_count=0,
                       watch_count=0, high_count=0)
        _save_digest_record(date_str, record)
        print(f"soccer_daily_digest: board stale/missing ({source}) - sent warning instead of a digest.")
        return record

    try:
        candidates = soccer_adapter.score_dabble_soccer_board(source=source, persist=True, notify=False,
                                                                snapshot_date=date_str)
    except Exception as e:
        attempted, delivered, status, error = _post_discord(
            content=_stale_warning_message(age_hours, board_generated_at, error=f"scoring failed: {e}"))
        record.update(discord_attempted=attempted, discord_delivered=delivered,
                       discord_http_status=status, discord_error=error, candidate_count=0,
                       watch_count=0, high_count=0)
        _save_digest_record(date_str, record)
        print(f"soccer_daily_digest: scoring pipeline raised ({e}) - sent warning instead of a digest.")
        return record

    total_live = len(candidates)
    if total_live == 0:
        attempted, delivered, status, error = _post_discord(content=_zero_candidate_message())
        record.update(discord_attempted=attempted, discord_delivered=delivered,
                       discord_http_status=status, discord_error=error,
                       candidate_count=0, watch_count=0, high_count=0)
        _save_digest_record(date_str, record)
        print("soccer_daily_digest: zero live Dabble soccer candidates - sent the alive-signal message.")
        return record

    shown, watch_count, high_count = select_digest_candidates(candidates)
    local_label = now.strftime("%I:%M %p UTC").lstrip("0")
    embeds = build_digest_embeds(shown, watch_count, high_count, total_live, local_label)
    attempted, delivered, status, error = _post_discord(embeds=embeds)

    record.update(discord_attempted=attempted, discord_delivered=delivered, discord_http_status=status,
                   discord_error=error, candidate_count=total_live, watch_count=watch_count,
                   high_count=high_count)
    _save_digest_record(date_str, record)
    print(f"soccer_daily_digest: {total_live} live candidates ({watch_count} WATCH, {high_count} HIGH) - "
          f"discord_attempted={attempted} discord_delivered={delivered}")
    return record


def digest_status(date_str=None):
    """python soccer_dns.py --digest-status"""
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    record = _load_digest_record(date_str)
    print(f"Date                       : {date_str}")
    print(f"Today's digest generated   : {'YES' if record else 'NO'}")
    if not record:
        return record
    print(f"Discord delivered          : {'YES' if record.get('discord_delivered') else 'NO'}")
    print(f"Delivered at               : {record.get('generated_at')}")
    print(f"Candidate count            : {record.get('candidate_count')}")
    print(f"Watch / High               : {record.get('watch_count')} / {record.get('high_count')}")
    print(f"Discord attempted          : {record.get('discord_attempted')}")
    print(f"Last error                 : {record.get('discord_error')}")
    return record


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Soccer DNS daily Discord digest")
    parser.add_argument("--send", action="store_true", help="run the full flow and send today's digest")
    parser.add_argument("--force", action="store_true", help="resend even if already delivered today")
    parser.add_argument("--status", action="store_true", help="print today's digest delivery status")
    parser.add_argument("--test-discord", action="store_true", help="send a plain health-check message")
    parser.add_argument("--source", default=None)
    args = parser.parse_args()

    if args.test_discord:
        send_test_message()
    elif args.status:
        digest_status()
    elif args.send:
        run_daily_digest(force=args.force, source=args.source)
    else:
        parser.error("specify --send, --status, or --test-discord")


if __name__ == "__main__":
    main()
