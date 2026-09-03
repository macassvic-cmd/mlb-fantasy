"""
Deadline check for the MLB pipeline's daily fetch (Phase added 2026-09-03,
see .github/workflows/pipeline.yml's CRON STRATEGY comment for the full
story - GitHub's own scheduler was silently dropping the morning fetch
attempts on 6+ separate days in a row despite a dense every-20-minute
retry schedule covering 12:00-18:40 UTC).

Run once, by the workflow's dedicated `deadline_alert` job (cron '0 17
* * *', decoupled from the main `run` job so it can't be skipped by
anything that job does). Posts a Discord alert if today's data/{date}.json
is missing or still older than the same 3-hour staleness floor
pipeline.yml's own "Check for fresh pipeline data" step uses - a
phone-reachable signal that every scheduled attempt so far this
morning failed, instead of the user discovering a stale dashboard later.

Deliberately a standalone script, not a YAML-embedded heredoc: GitHub
Actions' `run: |` block scalar forces every line (including a heredoc's
closing delimiter) to carry the step's own indentation, which a plain
bash heredoc terminator can't match without unreliable indentation
gymnastics - a real file sidesteps that entirely and is testable locally.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

STALE_AFTER_SECONDS = 10800  # 3h - same floor pipeline.yml's freshness gate uses


def is_stale(data_path):
    if not os.path.exists(data_path):
        return True
    out = subprocess.run(
        ["git", "log", "-1", "--format=%ct", "--", data_path],
        capture_output=True, text=True,
    )
    ts = out.stdout.strip()
    if not ts:
        return True
    return (time.time() - int(ts)) >= STALE_AFTER_SECONDS


def send_alert(webhook_url, today):
    body = {"embeds": [{
        "title": "\U0001F534 Pipeline deadline missed",
        "description": (
            f"No fresh `data/{today}.json` by the 17:00 UTC deadline - "
            "every scheduled fetch attempt this morning appears to have "
            "been dropped or failed. There's still time left in the "
            "afternoon backup window (19:41/21:41 UTC), but you may want "
            "to check `gh run list --workflow=pipeline.yml` or trigger "
            "one manually: `gh workflow run pipeline.yml -f run_type=pipeline`."
        ),
        "color": 0xE74C3C,
    }]}
    req = urllib.request.Request(
        webhook_url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=15)


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data_path = os.path.join("data", f"{today}.json")

    if not is_stale(data_path):
        print(f"OK: {data_path} is fresh.")
        return

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print(f"STALE ({data_path} missing or >={STALE_AFTER_SECONDS}s old) but DISCORD_WEBHOOK_URL is unset - no-op.")
        return

    try:
        send_alert(webhook_url, today)
        print(f"ALERTED: {data_path} missing or stale as of the 17:00 UTC deadline.")
    except Exception as e:
        print(f"Discord alert POST failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
