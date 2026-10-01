"""
Freshness gate for .github/workflows/pipeline.yml's "Check for fresh
pipeline data" step - decides whether this fire should skip (today's
data was already fetched recently) or run the full pipeline.

History of what this got wrong, in order:
  1. Originally compared against data/{date}.json's file mtime -
     actions/checkout resets every file's mtime to checkout time, so it
     always read as a few seconds old and silently skipped every fire
     after the first (fixed by switching to `git log`'s commit time).
  2. The `git log` version (fixed 2026-08-27) broke a different way,
     found 2026-09-04: a same-day rerun very often produces BYTE-
     IDENTICAL output to an earlier fetch that day (most source stats
     don't change before the slate's games start), so `git diff --cached
     --quiet` finds nothing to commit and data/{date}.json's last-commit
     timestamp never advances. That made the gate see EVERY fire past the
     3h mark as "stale" forever, once content stopped changing - the
     opposite of its job, and directly undermined the dense CI retry
     schedule's "redundant fires are nearly free" premise (5 of 6 fires
     that day did a full wasted ~15min refetch).

Fix: read data/.pipeline_last_fetch.json instead - pipeline.py writes it
with a fresh timestamp on EVERY successful run regardless of whether the
player data itself changed, so this never depends on git commit history
or file mtimes at all, just the fetcher's own self-reported completion
time.

  3. Found 2026-09-14: `today` here was computed as a bare UTC date,
     disagreeing with pipeline.py/.github/workflows/pipeline.yml's
     Pacific-anchored "today" (see scrapers.mlb_api.mlb_today_str) for
     roughly 17:00-23:59 PT every evening - UTC's calendar date is
     already tomorrow's during that window. This gate would then see
     the real fetch marker (correctly dated in Pacific terms) as "for a
     different day," always report skip=false, and force a full
     redundant refetch on every fire in that window - not a data-
     correctness bug (pipeline.py still fetches the right Pacific date
     either way, per its own --date argument from the workflow's
     rundate step), but silently defeated the "redundant fires are
     nearly free" premise the dense retry schedule depends on. Now
     shares the exact same date computation as everything else.

  4. Found 2026-09-15: a pure 3h time gate has no idea whether a real
     lineup update happened since the last fetch. The morning's dense
     schedule ends ~11:40am PDT; confirmed live that day's last real
     fetch landed ~10:40am PT, the two afternoon backup fires either
     found data <3h old and skipped (12:49pm PT) or were silently
     dropped by GitHub's own scheduler (2:41pm PT - the exact
     reliability problem the CRON STRATEGY comment in pipeline.yml
     already documents for the morning window), and the dashboard sat
     stale through first pitch with lineups that had since posted or
     changed. Fixed below: even a <3h-old fetch does NOT skip if any of
     today's games hasn't started yet and still has an unconfirmed
     lineup - the 3h ceiling stays as a backstop for the "nothing left
     to check" case, but is no longer the only thing deciding "stale."

  5. Found 2026-10-01 (postseason revival): on a day with NO games the
     gate always said "run", pipeline.py logged "No games found" and
     exited 0 without writing data/{date}.json, and the workflow's fetch
     step then failed the job - on every single fire (all six runs on
     2026-09-28, the off day after the regular season, failed this way).
     The regular season has almost no empty days so this never showed;
     the postseason has one every few days (travel days, series that end
     early). Fixed below: if MLB's schedule positively reports zero games
     for today, skip. An API failure is NOT treated as "no games" - it
     falls through to the normal checks, so an outage can't silently
     suppress a real slate's fetch.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

from scrapers.mlb_api import get_games, mlb_today_str

STALE_AFTER_SECONDS = 10800  # 3h


def _no_games_today(today):
    """True only if MLB's schedule answered and listed zero games for
    `today` (item 5 above). False on any API failure - not knowing is
    not the same as an empty slate, and the fetch step's own "did not
    produce data/{date}.json" failure is the right outcome then."""
    try:
        return len(get_games(today)) == 0
    except Exception as e:
        print(f"Schedule check failed ({type(e).__name__}: {e}) - not treating as a no-game day.", file=sys.stderr)
        return False


def _any_unconfirmed_lineup_pending(today):
    """True if data/{today}.json has at least one player whose game
    hasn't started yet and whose lineup is still not confirmed - the
    exact case a plain elapsed-time gate can't see (item 4 above). False
    (never blocks a skip) on any missing/malformed data, since this is
    an ADDITIONAL reason to run, not the only one - the elapsed-time
    check above still catches a genuinely stale fetch either way."""
    path = os.path.join("data", f"{today}.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            players = json.load(f)
    except Exception:
        return False

    now = datetime.now(timezone.utc)
    for p in players:
        game_time_raw = p.get("game_date_utc")
        if not game_time_raw:
            continue
        try:
            game_time = datetime.fromisoformat(game_time_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if game_time <= now:
            continue  # already started - too late for a lineup refresh to matter
        if not p.get("lineup_confirmed", False):
            return True
    return False


def main():
    today = mlb_today_str()
    marker_path = os.path.join("data", ".pipeline_last_fetch.json")

    skip = False
    reason = f"No fetch marker found - running pipeline for {today}."

    if _no_games_today(today):
        skip = True
        reason = f"No MLB games scheduled for {today} - nothing to fetch, skipping this fire."
    elif os.path.exists(marker_path):
        with open(marker_path, encoding="utf-8") as f:
            marker = json.load(f)
        if marker.get("date") != today:
            reason = f"Fetch marker is for {marker.get('date')}, not {today} - running pipeline."
        else:
            fetched_at = datetime.fromisoformat(marker["fetched_at_utc"])
            age = time.time() - fetched_at.timestamp()
            if age < STALE_AFTER_SECONDS:
                if _any_unconfirmed_lineup_pending(today):
                    reason = (f"Data for {today} was fetched {age:.0f}s ago (<3h) BUT at least one "
                              f"game hasn't started with an unconfirmed lineup - running pipeline anyway.")
                else:
                    skip = True
                    reason = f"Data for {today} was fetched {age:.0f}s ago (<3h) - skipping this fire."
            else:
                reason = f"Data for {today} was fetched {age:.0f}s ago (>=3h) - running pipeline."

    print(reason)

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"skip={'true' if skip else 'false'}\n")
    else:
        print("WARNING: GITHUB_OUTPUT not set (not running in Actions?) - skip output not written.", file=sys.stderr)


if __name__ == "__main__":
    main()
