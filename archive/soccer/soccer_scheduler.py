"""
Kickoff-aware intraday scheduler for Soccer DNS (2026-09-16) - decides,
per fixture, how often it needs re-checking based on time to kickoff, and
gives the Dabble board its own separate refresh cadence. Same "dense cron
+ fast runtime gate" philosophy already proven for the MLB pipeline
(check_pipeline_freshness.py) applied to soccer's own, more granular
3-band schedule.

Root cause this exists for (found live 2026-09-16): soccer_dns_daily_
digest.yml's only cadence was two once-a-day cron fires gated by a narrow
1-hour "is it 7am Pacific" window - `gh run list` showed both of a given
day's fires landing 3-4h late (GitHub's documented scheduling delay,
already known from the MLB pipeline), missing that window entirely and
producing a full day with zero real digest runs. There is no equivalent
"redundant fires are free" safety net for a once-daily job the way there
is for a dense schedule - this module is that safety net, generalized to
real-time alerting through the whole day, not just the morning digest.

Bands (config values, per item 2's spec):
  >24h to kickoff   - check every FAR_INTERVAL_MINUTES (2-3h)
  24h-3h to kickoff - check every MID_INTERVAL_MINUTES (30-60 min)
  final 3h          - check every NEAR_INTERVAL_MINUTES (15-20 min)
  at/after kickoff  - never (nothing left to predict)

The Dabble board itself is throttled separately (item 2): at most every
BOARD_MIN_INTERVAL_MINUTES (30-60 min), PLUS one forced refresh within
BOARD_KICKOFF_PROXIMITY_MINUTES (90 min) of any fixture's kickoff, so a
late lineup change close to kickoff isn't missed just because the last
board fetch happened to be recent.

State (data/soccer_scheduler_state.json) tracks last_checked_at (the
last full enrichment+scoring+alert pass), last_board_fetch_at, and
kickoff_proximity_done (fixture_ids that already got their forced pre-
kickoff board refresh, so it fires once per fixture, not every cycle
inside the 90-minute window).
"""

import json
import os
from datetime import datetime, timezone

FAR_HOURS = 24
NEAR_HOURS = 3

FAR_INTERVAL_MINUTES = 150   # >24h to kickoff: every 2-3h
MID_INTERVAL_MINUTES = 45    # 24h-3h to kickoff: every 30-60 min
NEAR_INTERVAL_MINUTES = 17   # final 3h: every 15-20 min

BOARD_MIN_INTERVAL_MINUTES = 45       # Dabble board: at most every 30-60 min
BOARD_KICKOFF_PROXIMITY_MINUTES = 90  # plus one forced refresh within this long of kickoff

STATE_PATH = os.path.join("data", "soccer_scheduler_state.json")


def _parse(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def interval_minutes_for(hours_to_kickoff):
    """Minutes between checks for a fixture this many hours from kickoff,
    or None if kickoff has already passed (or hours_to_kickoff itself is
    unknown) - never checked again either way."""
    if hours_to_kickoff is None or hours_to_kickoff <= 0:
        return None
    if hours_to_kickoff > FAR_HOURS:
        return FAR_INTERVAL_MINUTES
    if hours_to_kickoff > NEAR_HOURS:
        return MID_INTERVAL_MINUTES
    return NEAR_INTERVAL_MINUTES


def any_fixture_due(fixtures, last_checked_at_iso, now=None):
    """fixtures: [{"kickoff": iso}, ...] (fixture_id not needed here -
    every fixture shares the same last_checked_at, since one board-wide
    scoring pass checks all of them at once). True if AT LEAST ONE
    upcoming fixture's own band interval has elapsed since the last
    check - a fixture entering a tighter band (e.g. crossing from 24h+
    into the 24h-3h window) becomes due again even if the overall board
    was checked recently under the looser band."""
    now = now or datetime.now(timezone.utc)
    last_checked = _parse(last_checked_at_iso)
    minutes_since = None if last_checked is None else (now - last_checked).total_seconds() / 60

    for f in fixtures:
        kickoff = _parse(f.get("kickoff"))
        if kickoff is None or kickoff <= now:
            continue
        hours_to_kickoff = (kickoff - now).total_seconds() / 3600
        interval = interval_minutes_for(hours_to_kickoff)
        if interval is None:
            continue
        if minutes_since is None or minutes_since >= interval:
            return True
    return False


def board_fetch_due(last_board_fetch_iso, fixtures, kickoff_proximity_done, now=None):
    """(due: bool, reason: str). kickoff_proximity_done is a list/set of
    fixture_ids that already received their forced pre-kickoff refresh -
    callers should add the fixture_id(s) responsible for reason.startswith
    ("kickoff_proximity:") to that set after actually fetching, so the
    same fixture doesn't force a refetch on every cycle inside its
    90-minute window."""
    now = now or datetime.now(timezone.utc)
    last_fetch = _parse(last_board_fetch_iso)
    if last_fetch is None:
        return True, "never_fetched"

    minutes_since = (now - last_fetch).total_seconds() / 60
    if minutes_since >= BOARD_MIN_INTERVAL_MINUTES:
        return True, "interval_elapsed"

    for f in fixtures:
        fid = f.get("fixture_id")
        if fid is None or fid in kickoff_proximity_done:
            continue
        kickoff = _parse(f.get("kickoff"))
        if kickoff is None:
            continue
        minutes_to_kickoff = (kickoff - now).total_seconds() / 60
        if 0 < minutes_to_kickoff <= BOARD_KICKOFF_PROXIMITY_MINUTES:
            return True, f"kickoff_proximity:{fid}"

    return False, "not_due"


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
            state.setdefault("kickoff_proximity_done", [])
            return state
        except Exception:
            pass
    return {"last_checked_at": None, "last_board_fetch_at": None, "kickoff_proximity_done": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH) or ".", exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def current_fixtures(board_path=None):
    """[{"fixture_id":, "kickoff":}, ...] - one entry per distinct
    fixture currently on the live Dabble board, derived from soccer_
    adapter's own prop loader rather than a second, separate board
    parse - the scheduler asks the same board data the scorer already
    reads, never a stale or independently-fetched copy."""
    import soccer_adapter
    source = board_path or soccer_adapter.DEFAULT_BOARD_PATH
    props = soccer_adapter.load_dabble_soccer_props(source)
    fixtures = {}
    for p in props:
        fid = p.get("fixture_id")
        if fid and fid not in fixtures:
            fixtures[fid] = {"fixture_id": fid, "kickoff": p.get("event_date")}
    return list(fixtures.values())
