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
import sys
import time
import urllib.request
from datetime import datetime, timezone

STALE_AFTER_SECONDS = 10800  # 3h - same floor check_pipeline_freshness.py uses

MARKER_PATH = os.path.join("data", ".pipeline_last_fetch.json")


def is_stale(today):
    # Reads the same self-reported marker check_pipeline_freshness.py
    # does, not git commit history - a same-day rerun with byte-identical
    # output never advances a file's last-commit timestamp (see that
    # module's docstring for the 2026-09-04 incident this caused), which
    # would make THIS check just as blind to "did a fetch actually just
    # happen" as the freshness gate originally was.
    if not os.path.exists(MARKER_PATH):
        return True
    with open(MARKER_PATH, encoding="utf-8") as f:
        marker = json.load(f)
    if marker.get("date") != today:
        return True
    fetched_at = datetime.fromisoformat(marker["fetched_at_utc"])
    return (time.time() - fetched_at.timestamp()) >= STALE_AFTER_SECONDS


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
    # User-Agent is required, not cosmetic: Discord's webhook endpoint is
    # fronted by Cloudflare, which 403s (error 1010 - "banned based on
    # your browser's signature") a request carrying urllib's default
    # "Python-urllib/3.x" UA. Confirmed live 2026-09-04 - the first real
    # deadline check correctly detected stale data and tried to alert,
    # but the POST itself failed with exactly this error, silently
    # (`run.py` still exits nonzero and the workflow shows a failed job,
    # but no Discord message ever arrives - the alert fails exactly the
    # one time it's supposed to matter). scrapers/betr.py, market_lines.py
    # etc. already set this same header for the same reason.
    req = urllib.request.Request(
        webhook_url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    urllib.request.urlopen(req, timeout=15)


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not is_stale(today):
        print(f"OK: data for {today} is fresh.")
        return

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print(f"STALE (no fresh fetch marker for {today}) but DISCORD_WEBHOOK_URL is unset - no-op.")
        return

    try:
        send_alert(webhook_url, today)
        print(f"ALERTED: no fresh fetch marker for {today} as of the 17:00 UTC deadline.")
    except Exception as e:
        print(f"Discord alert POST failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
