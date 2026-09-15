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
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

# 2026-09-14 production-scheduling fix: the board used to be refreshable
# ONLY via an external script at this Windows-only path, which made "do
# not rely on my PC being awake" structurally impossible - a GitHub
# Actions runner can't see C:\platform-tools\. scrapers.dabble_soccer_
# producer is the same fetch logic checked INTO the repo, callable via a
# plain import on any OS - see _attempt_board_refresh, which tries that
# first and only falls back to this external path for a local machine
# that happens to still have it (harmless, never required).
EXTERNAL_PRODUCER_PATH = r"C:\platform-tools\dabble_board_producer.py"

DIGEST_DIR = os.path.join("data", "soccer_daily_digest")
WEBHOOK_ENV_VAR = "DISCORD_SOCCER_DNS_WEBHOOK_URL"  # same channel/env var as soccer_alerts.py's real-time alerts

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


FALLBACK_WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"


def _webhook_url():
    """Same fallback as soccer_alerts.py's _webhook_url - see that
    function's docstring."""
    return os.environ.get(WEBHOOK_ENV_VAR) or os.environ.get(FALLBACK_WEBHOOK_ENV_VAR)


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
        "watch_count": None, "alert_count": None, "high_count": None, "discord_attempted": False,
        "discord_delivered": False, "discord_http_status": None, "discord_error": None,
    }


# ---------------------------------------------------------------------------
# Board freshness - "refresh Dabble board -> ... -> Discord"; a failed or
# stale refresh must produce a system warning, never a digest built off
# old data (see module docstring's FLOW section).
# ---------------------------------------------------------------------------

def _attempt_board_refresh(output_path):
    """Fetches a genuinely fresh Dabble soccer board, preferring the
    portable in-repo producer (scrapers.dabble_soccer_producer - works
    identically on any OS, including a GitHub Actions runner) and
    falling back to the external Windows-only script only if present
    (a local dev convenience, never required). Returns True only on an
    actual successful fetch+write; False (logged, never raised) covers
    every failure mode - either way the caller falls through to
    _board_freshness's own check rather than trusting this call's
    success blindly."""
    try:
        from scrapers.dabble_soccer_producer import produce_soccer_board
        payload = produce_soccer_board(output_path)
        print(f"soccer_daily_digest: fresh Dabble fetch OK via in-repo producer - "
              f"{len(payload.get('props', []))} props across {len(payload.get('competitions', []))} competitions.")
        return True
    except Exception as e:
        print(f"soccer_daily_digest: in-repo producer fetch failed ({e}) - trying the external local script.")

    if not os.path.exists(EXTERNAL_PRODUCER_PATH):
        print(f"soccer_daily_digest: external board producer not found at {EXTERNAL_PRODUCER_PATH} - "
              f"falling back to whatever board is already on disk (staleness is still checked next).")
        return False
    try:
        subprocess.run([sys.executable, EXTERNAL_PRODUCER_PATH, "--sport", "soccer", "--output", output_path],
                        timeout=90, check=True, capture_output=True, text=True)
        return True
    except Exception as e:
        print(f"soccer_daily_digest: external board producer invocation failed (non-fatal): {e}")
        return False


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
    """(shown, watch_count, alert_count, high_count) - sorted by
    combined_priority DESC, at least MIN_CANDIDATES_SHOWN plus every
    Watch-or-higher candidate even if that makes the list longer
    (never truncates a real Watch+ risk just to hit a round number).

    watch_count/alert_count/high_count come from soccer_dashboard.
    tier_counts - the SAME function docs/soccer-dns.html's own chips use
    - rather than this module's own separate cumulative dns_score>=70
    count it kept before (2026-09-14 item 5: found live that the two
    disagreed - the digest said "7 WATCH", the dashboard said "0 Watch",
    both "correct" for their own different definition of the word).
    watch/alert are mutually exclusive bands that partition with high;
    the display cutoff sums all three, which is exactly the old
    cumulative dns_score>=70 count (the partition covers the same
    candidates, just broken into named bands now)."""
    import soccer_dashboard
    ranked = sorted(candidates, key=lambda c: c.get("combined_priority") or 0, reverse=True)
    _live, watch_count, alert_count, high_count, _critical = soccer_dashboard.tier_counts(ranked)

    cutoff = max(MIN_CANDIDATES_SHOWN, watch_count + alert_count + high_count)
    shown = ranked[:cutoff]
    return shown, watch_count, alert_count, high_count


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


def build_digest_embeds(shown, watch_count, alert_count, high_count, total_live, generated_at_local_label):
    """Discord embeds for the full digest - chunked to stay under the
    4096-char-per-embed-description limit (same discipline as soccer_
    dns.py's build_digest_embeds for the MLB-style injury digest).

    Labels match docs/soccer-dns.html's own chips exactly (Watch 70-74,
    Alert 75-84, High 85+) - both now come from soccer_dashboard.
    tier_counts, so they can no longer disagree (item 5, 2026-09-14)."""
    header = (f"🟢 {total_live} Dabble players live\n"
              f"🎯 {watch_count} WATCH (70-74)\n"
              f"⚠️ {alert_count} ALERT (75-84)\n"
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

def _post_discord(content=None, embeds=None, source="daily_digest"):
    """(attempted: bool, delivered: bool, http_status: int_or_None,
    error: str_or_None). attempted is False only when no webhook is
    configured at all - a documented no-op, never conflated with a
    failed delivery attempt (see module docstring). Every attempt
    (success or failure) is recorded in discord_health.py's shared
    tracking regardless of source, so the dashboard's Discord-health
    section reflects both the digest and real-time alerts."""
    import discord_health

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
        # Discord's own response body, not an exception - low risk (it
        # never needs to echo the webhook URL back to the client that
        # already has it) but still redacted before being persisted/
        # returned, same blanket policy as the exception path below.
        error = None if delivered else discord_health.redact(resp.text[:300])
        discord_health.record_attempt(source, delivered, http_status=resp.status_code, error=error)
        return True, delivered, resp.status_code, error
    except Exception as e:
        # Exception type + HTTP status ONLY - never str(e) (2026-09-15
        # security fix): a requests exception commonly embeds the full
        # request URL, which for a Discord webhook POST IS a bearer
        # credential. record_attempt also redacts as a backstop, but the
        # message is built safe here in the first place.
        error = f"{type(e).__name__} (HTTP None - request never completed)"
        discord_health.record_attempt(source, False, http_status=None, error=error)
        return True, False, None, error


def send_test_message():
    """python soccer_dns.py --test-discord - manual health-check send,
    separate from a real digest/alert, for confirming the webhook itself
    works end to end."""
    attempted, delivered, status, error = _post_discord(
        content="✅ Soccer DNS Discord test message - webhook is reachable and delivering.", source="test")
    print(f"discord_attempted={attempted} discord_delivered={delivered} "
          f"http_status={status} error={error}")
    return {"discord_attempted": attempted, "discord_delivered": delivered,
            "discord_http_status": status, "discord_error": error}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def is_due_now(target_hour=7, window_hours=1, now=None):
    """True iff the current America/Los_Angeles wall-clock hour is
    within [target_hour, target_hour + window_hours) - the LA-anchored
    gate a GitHub Actions cron (UTC-only, and DST-blind) needs to
    actually mean "7am Pacific." The workflow schedules TWO UTC cron
    times (14:00 and 15:00 UTC) to cover both PDT and PST without
    drifting off Pacific wall-clock time across the DST transition; this
    function is what makes the OTHER of those two fires a correct no-op
    instead of a duplicate send (run_daily_digest's own once-per-day
    dedup would also catch a duplicate, but skipping before any network
    call is cheaper and makes the intent explicit in the workflow log)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_hour = now.astimezone(PACIFIC_TZ).hour
    return target_hour <= local_hour < target_hour + window_hours


def run_daily_digest(force=False, source=None, date_str=None, now=None):
    """The full flow: refresh -> enrich -> score -> persist -> digest ->
    Discord. Idempotent per calendar day unless force=True - a second
    invocation the same day that already delivered successfully is a
    no-op (returns the existing record) rather than double-posting."""
    import soccer_adapter

    now = now or datetime.now(timezone.utc)
    # Pacific-anchored, same lesson/bug class as scrapers.mlb_api.
    # mlb_today_str - a bare UTC date here would silently roll "today"
    # over up to 7-8 hours before Pacific evening does (confirmed live
    # 2026-09-14 running this at 22:53 PT / 05:53 UTC the next day - the
    # UTC-only version of this line stamped the digest "2026-09-15" while
    # every human involved still called it "today, the 14th").
    date_str = date_str or now.astimezone(PACIFIC_TZ).strftime("%Y-%m-%d")
    source = source or soccer_adapter.DEFAULT_BOARD_PATH

    existing = _load_digest_record(date_str)
    if existing and existing.get("discord_delivered") and not force:
        print(f"soccer_daily_digest: {date_str} already delivered at generation time "
              f"{existing.get('generated_at')} - use --force to resend.")
        return existing

    record = _new_record(date_str)
    record["generated_at"] = now.isoformat()

    # Step 1 of the flow (item 5): attempt a real refresh BEFORE ever
    # checking/trusting whatever board file is already on disk.
    _attempt_board_refresh(source)

    fresh, age_hours, board_generated_at = _board_freshness(source)
    if not fresh:
        attempted, delivered, status, error = _post_discord(
            content=_stale_warning_message(age_hours, board_generated_at))
        record.update(discord_attempted=attempted, discord_delivered=delivered,
                       discord_http_status=status, discord_error=error, candidate_count=0,
                       watch_count=0, alert_count=0, high_count=0)
        _save_digest_record(date_str, record)
        print(f"soccer_daily_digest: board stale/missing ({source}) - sent warning instead of a digest.")
        return record

    try:
        # notify=True: real-time threshold/news-signal alerts fire off
        # this SAME scoring pass (soccer_alerts.notify_soccer_candidates),
        # per the documented flow (fetch -> enrich -> score -> persist ->
        # alerts -> digest -> dashboard) - one scoring pass feeds all of
        # it rather than re-scoring separately for each step.
        candidates = soccer_adapter.score_dabble_soccer_board(source=source, persist=True, notify=True,
                                                                snapshot_date=date_str)
    except Exception as e:
        attempted, delivered, status, error = _post_discord(
            content=_stale_warning_message(age_hours, board_generated_at, error=f"scoring failed: {e}"))
        record.update(discord_attempted=attempted, discord_delivered=delivered,
                       discord_http_status=status, discord_error=error, candidate_count=0,
                       watch_count=0, alert_count=0, high_count=0)
        _save_digest_record(date_str, record)
        print(f"soccer_daily_digest: scoring pipeline raised ({e}) - sent warning instead of a digest.")
        return record

    try:
        import soccer_dashboard
        soccer_dashboard.generate(candidates, date_str=date_str)
        print("soccer_daily_digest: regenerated docs/soccer-dns.html from this run's candidates.")
    except Exception as e:
        print(f"soccer_daily_digest: dashboard regeneration failed (non-fatal, digest still sends): {e}")

    # Grade the last 7 days' alerts every run (item 4, 2026-09-14; swept
    # to a 7-day window 2026-09-15) - grade_alerts alone never retries a
    # date once "yesterday" moves past it, so a record that couldn't be
    # graded on its first attempt (ESPN summary not posted yet) was
    # stuck ungraded forever - confirmed live: 43 of 76 2026-09-14
    # records were still pending a day later with only the single-date
    # call. grade_alerts_recent re-checks the whole window every run;
    # grade_alerts itself already skips anything already graded or any
    # date with no alerts file, so this is cheap. Non-fatal: a grading
    # failure must never block the digest itself from sending.
    try:
        import soccer_alerts
        soccer_alerts.grade_alerts_recent(days=7, as_of_date=date_str)
    except Exception as e:
        print(f"soccer_daily_digest: grading recent alerts failed (non-fatal): {e}")

    total_live = len(candidates)
    if total_live == 0:
        attempted, delivered, status, error = _post_discord(content=_zero_candidate_message())
        record.update(discord_attempted=attempted, discord_delivered=delivered,
                       discord_http_status=status, discord_error=error,
                       candidate_count=0, watch_count=0, alert_count=0, high_count=0)
        _save_digest_record(date_str, record)
        print("soccer_daily_digest: zero live Dabble soccer candidates - sent the alive-signal message.")
        return record

    shown, watch_count, alert_count, high_count = select_digest_candidates(candidates)
    local_label = now.strftime("%I:%M %p UTC").lstrip("0")
    embeds = build_digest_embeds(shown, watch_count, alert_count, high_count, total_live, local_label)
    attempted, delivered, status, error = _post_discord(embeds=embeds)

    record.update(discord_attempted=attempted, discord_delivered=delivered, discord_http_status=status,
                   discord_error=error, candidate_count=total_live, watch_count=watch_count,
                   alert_count=alert_count, high_count=high_count)
    _save_digest_record(date_str, record)
    print(f"soccer_daily_digest: {total_live} live candidates ({watch_count} WATCH, {alert_count} ALERT, "
          f"{high_count} HIGH) - discord_attempted={attempted} discord_delivered={delivered}")
    return record


def digest_status(date_str=None):
    """python soccer_dns.py --digest-status"""
    date_str = date_str or datetime.now(timezone.utc).astimezone(PACIFIC_TZ).strftime("%Y-%m-%d")
    record = _load_digest_record(date_str)
    print(f"Date                       : {date_str}")
    print(f"Today's digest generated   : {'YES' if record else 'NO'}")
    if not record:
        return record
    print(f"Discord delivered          : {'YES' if record.get('discord_delivered') else 'NO'}")
    print(f"Delivered at               : {record.get('generated_at')}")
    print(f"Candidate count            : {record.get('candidate_count')}")
    print(f"Watch / Alert / High       : {record.get('watch_count')} / {record.get('alert_count')} / "
          f"{record.get('high_count')}")
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
    parser.add_argument("--if-due", action="store_true",
                         help="with --send, only actually run if the current America/Los_Angeles hour is "
                              "within the target window - see is_due_now. Lets a dense multi-UTC-time cron "
                              "(covering PDT/PST) fire safely without sending twice a day.")
    parser.add_argument("--due-hour", type=int, default=7, help="target Pacific hour for --if-due (default 7 = 7am PT)")
    args = parser.parse_args()

    if args.test_discord:
        send_test_message()
    elif args.status:
        digest_status()
    elif args.send:
        if args.if_due and not is_due_now(target_hour=args.due_hour):
            now_pt = datetime.now(timezone.utc).astimezone(PACIFIC_TZ)
            print(f"soccer_daily_digest: not due yet (current Pacific time {now_pt.strftime('%H:%M %Z')}, "
                  f"target hour {args.due_hour}:00) - skipping this fire.")
            return
        run_daily_digest(force=args.force, source=args.source)
    else:
        parser.error("specify --send, --status, or --test-discord")


if __name__ == "__main__":
    main()
