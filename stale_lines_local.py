"""
Persistent local runner for the stale-line detector - polls every 30
seconds, indefinitely, as a substitute for GitHub Actions' */5 cron.

WHY THIS EXISTS (2026-09-01): the Tyler Stephenson case proved every
detection gate (team resolution, lineup hydration, second-source cross-
check, name normalization) works correctly - the miss was a 103-minute
GitHub Actions scheduling gap with zero polls, the same cron-reliability
pattern already documented for the main pipeline (see pipeline.yml's
CRON STRATEGY comment). The fix isn't better detection logic - it's not
depending on GitHub's scheduler for something this time-sensitive.

Reuses stale_lines.run_poll() UNCHANGED - this file is purely an
orchestration wrapper (loop cadence, heartbeat, crash alerting, git
persistence), never a second implementation of the detection logic.
Both this and the GitHub Actions workflow call the exact same function,
so there's only ever one place detection logic can drift.

DEPLOYMENT: run as a Windows Scheduled Task (see
setup_stale_lines_local_task.ps1) with S4U logon (same pattern as the
other MLB Fantasy tasks in this repo) triggered AtStartup/AtLogOn, no
execution time limit, auto-restart on failure - see that script's own
comments for the exact settings and why each one matters for a
long-running process specifically (default Task Scheduler settings
assume a task that runs briefly and exits, not one meant to run for
days).

GITHUB ACTIONS WORKFLOW STAYS RUNNING - this is additive redundant
coverage, not a replacement. Both write to the same data/stale_lines/
files and both commit/push to the same git repo/branch -
git_commit_and_push_if_changed() below mirrors the exact pull-rebase-
resolve-theirs-on-conflict dance .github/workflows/stale_lines.yml's own
commit step already uses, so a collision between the two self-resolves
the same way either side would handle it alone.

CACHE INVALIDATION: unlike a fresh-process-per-poll invocation (GitHub
Actions), this process lives for days - scrapers/mlb_api.py's roster/
40-man caches are documented as "process lifetime" caches, which would
otherwise mean "never refreshes again after the first poll" here. See
mlb_api.clear_per_poll_caches(), called every iteration before run_poll().

Reads DISCORD_WEBHOOK_URL from .env (python-dotenv) rather than a
GitHub Actions secret - add DISCORD_WEBHOOK_URL=... to .env yourself
(already gitignored, same as OPENWEATHER_API_KEY). stale_lines.py's own
webhook lookup just reads the environment either way, so no detection
code needed to change.
"""

import json
import logging
import os
import subprocess
import time
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

import stale_lines as sl
from scrapers.mlb_api import clear_per_poll_caches

POLL_INTERVAL_SECONDS = 30

# Decoupled from POLL_INTERVAL_SECONDS on purpose - detection/alerting
# needs to happen every 30s (that's the whole point), but committing to
# git every 30s would produce ~2,880 commits/day even on a quiet day.
# Discord alerts fire immediately regardless of this - only the git
# persistence of state.json/events.jsonl is throttled.
GIT_PUSH_INTERVAL_SECONDS = 90

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join("data", "stale_lines")
HEARTBEAT_PATH = os.path.join(LOCAL_DIR, "local_heartbeat.json")
LOG_PATH = "stale_lines_local.log"

# Same bound report.py's deploy_to_github_pages uses - a hung git command
# must never permanently stall a persistent loop.
GIT_SUBPROCESS_TIMEOUT = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger("stale_lines_local")


def write_heartbeat(ok, summary=None, error=None):
    """Atomic write (tmp file + os.replace) so the watchdog never reads
    a half-written file mid-update."""
    os.makedirs(LOCAL_DIR, exist_ok=True)
    payload = {
        "last_poll_utc": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "error": error,
        "counters": (summary or {}).get("counters"),
        "early_signals_new": (summary or {}).get("early_signals", {}).get("new_this_poll"),
    }
    tmp_path = HEARTBEAT_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, HEARTBEAT_PATH)


def _git(args):
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=GIT_SUBPROCESS_TIMEOUT,
    )


def git_commit_and_push_if_changed():
    """Mirrors .github/workflows/stale_lines.yml's own commit step
    exactly (same add/diff-check/commit/pull-rebase/resolve-theirs-on-
    conflict/push sequence) so a collision between this process and that
    workflow both trying to update data/stale_lines/ at the same moment
    resolves the same way either side already handles alone. Best-
    effort - logs and returns on any failure rather than raising, since
    a git hiccup must never stop the detection loop itself."""
    try:
        _git(["add", "data/stale_lines/"])
        diff = _git(["diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            return  # nothing changed
        commit = _git(["commit", "-m", f"Local stale-line poll {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"])
        if commit.returncode != 0:
            logger.warning("git commit failed: %s", commit.stderr.strip())
            return
        pull = _git(["pull", "--rebase", "origin", "main"])
        if pull.returncode != 0:
            logger.warning("git pull --rebase hit a conflict - resolving by taking this process's version of state files: %s", pull.stderr.strip())
            conflicts = _git(["diff", "--name-only", "--diff-filter=U"])
            for f in conflicts.stdout.splitlines():
                f = f.strip()
                if not f:
                    continue
                theirs = _git(["checkout", "--theirs", "--", f])
                if theirs.returncode == 0:
                    _git(["add", "--", f])
                else:
                    _git(["rm", "-f", "--", f])
            continue_res = _git(["rebase", "--continue"])
            if continue_res.returncode != 0:
                logger.error("git rebase --continue failed: %s - aborting rebase to avoid leaving the repo in a broken state", continue_res.stderr.strip())
                _git(["rebase", "--abort"])
                return
        push = _git(["push"])
        if push.returncode != 0:
            logger.warning("git push failed: %s", push.stderr.strip())
    except subprocess.TimeoutExpired as e:
        logger.error("git operation timed out after %ss: %s", GIT_SUBPROCESS_TIMEOUT, e)
    except Exception as e:
        logger.error("git_commit_and_push_if_changed failed: %s", e)


def run_forever():
    logger.info("stale_lines_local starting - polling every %ss, git push throttled to every %ss", POLL_INTERVAL_SECONDS, GIT_PUSH_INTERVAL_SECONDS)
    if not sl._discord_webhook_url():
        logger.warning("DISCORD_WEBHOOK_URL is not set (checked .env and environment) - notifications will be a no-op.")

    sl.post_system_alert(
        "\U0001F7E2 stale_lines_local started",
        f"Persistent local runner started at {datetime.now(timezone.utc).isoformat()}.",
        color=0x2ECC71,
    )

    last_push = 0.0
    consecutive_failures = 0
    while True:
        iter_start = time.monotonic()
        try:
            clear_per_poll_caches()
            summary = sl.run_poll()
            write_heartbeat(ok=True, summary=summary)
            logger.info(
                "poll ok - flags: %s active/%s total, early_signals new: %s, record: %s",
                summary["active_unresolved_flags"], summary["total_tracked_flags"],
                summary["early_signals"]["new_this_poll"], summary["record"],
            )
            consecutive_failures = 0
        except Exception as e:
            consecutive_failures += 1
            logger.error("poll failed (%s consecutive): %s\n%s", consecutive_failures, e, traceback.format_exc())
            write_heartbeat(ok=False, error=str(e))
            sl.post_system_alert(
                "\U0001F534 stale_lines_local poll crashed",
                f"Poll failed at {datetime.now(timezone.utc).isoformat()} ({consecutive_failures} consecutive failure(s)):\n```{e}```",
            )

        now_monotonic = time.monotonic()
        if now_monotonic - last_push >= GIT_PUSH_INTERVAL_SECONDS:
            git_commit_and_push_if_changed()
            last_push = now_monotonic

        elapsed = time.monotonic() - iter_start
        time.sleep(max(1.0, POLL_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        logger.info("stale_lines_local stopped (KeyboardInterrupt).")
    except Exception as e:
        logger.critical("stale_lines_local exiting on unhandled exception: %s\n%s", e, traceback.format_exc())
        try:
            sl.post_system_alert("\U0001F480 stale_lines_local process died", f"Unhandled exception, process is exiting:\n```{e}```")
        except Exception:
            pass
        raise
