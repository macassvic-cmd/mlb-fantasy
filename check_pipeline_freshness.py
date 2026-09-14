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
"""

import json
import os
import sys
import time
from datetime import datetime

from scrapers.mlb_api import mlb_today_str

STALE_AFTER_SECONDS = 10800  # 3h


def main():
    today = mlb_today_str()
    marker_path = os.path.join("data", ".pipeline_last_fetch.json")

    skip = False
    reason = f"No fetch marker found - running pipeline for {today}."

    if os.path.exists(marker_path):
        with open(marker_path, encoding="utf-8") as f:
            marker = json.load(f)
        if marker.get("date") != today:
            reason = f"Fetch marker is for {marker.get('date')}, not {today} - running pipeline."
        else:
            fetched_at = datetime.fromisoformat(marker["fetched_at_utc"])
            age = time.time() - fetched_at.timestamp()
            if age < STALE_AFTER_SECONDS:
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
