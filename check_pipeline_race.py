"""
Race check for pipeline.yml's "Commit and push all changes" step.

Pipeline steps can take up to 90 minutes, so two fires can genuinely
overlap. The original version of this check asked only "does
data/{date}.json exist on origin/main?" - true for the rest of the day
after the very first fetch, so it discarded EVERY later-in-the-day
refetch's work too, not just a genuine concurrent race. Found live
2026-09-04 while validating the freshness-marker fix: a manually
triggered validation run fetched fresh data successfully, then this
check discarded it anyway because *a* data/2026-09-04.json already
existed on origin (from that morning's first fetch, hours earlier) -
existence was never evidence of a race, just evidence that the day had
started.

Fix: compare TIMESTAMPS. Exit 0 (discard - a real race happened) only if
origin/main's data/.pipeline_last_fetch.json marker is strictly newer
than when THIS run started. If origin has no marker yet, or its marker
predates this run's start, our fetch has genuinely new information and
should be pushed even though a same-named file already exists.
"""

import json
import subprocess
import sys
from datetime import datetime


def main():
    if len(sys.argv) != 2:
        print("usage: check_pipeline_race.py <run_start_utc ISO8601>", file=sys.stderr)
        sys.exit(2)  # neither discard nor proceed cleanly - treat as error, not a race

    run_start = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))

    out = subprocess.run(
        ["git", "show", "origin/main:data/.pipeline_last_fetch.json"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        print("No fetch marker on origin/main yet - no race, proceeding to push.")
        sys.exit(1)

    try:
        marker = json.loads(out.stdout)
        origin_fetched_at = datetime.fromisoformat(marker["fetched_at_utc"])
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"Could not parse origin's fetch marker ({e}) - no race, proceeding to push.")
        sys.exit(1)

    if origin_fetched_at > run_start:
        print(f"origin/main's fetch marker ({origin_fetched_at.isoformat()}) is newer than "
              f"this run's start ({run_start.isoformat()}) - another run finished a newer "
              f"fetch while we worked. Discarding local changes.")
        sys.exit(0)

    print(f"origin/main's fetch marker ({origin_fetched_at.isoformat()}) predates this run's "
          f"start ({run_start.isoformat()}) - no race, proceeding to push.")
    sys.exit(1)


if __name__ == "__main__":
    main()
