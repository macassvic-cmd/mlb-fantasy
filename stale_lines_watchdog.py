"""
Lightweight watchdog for stale_lines_local.py - checks whether the local
runner's heartbeat is stale (no successful poll recorded in the last
STALL_THRESHOLD_SECONDS) and, if so, alerts Discord and attempts to
restart the runner's Scheduled Task.

Deliberately NOT part of stale_lines_local.py itself - a truly HUNG (not
crashed) process can't detect its own hang; only something external,
checking wall-clock time against a heartbeat file, can. Run as its OWN
Scheduled Task (see setup_stale_lines_local_task.ps1), triggered every 5
minutes, that starts, checks, and exits - not persistent like the
runner itself.

Dedup: only alerts on the TRANSITION into a stalled state, not every 5
minutes for the same ongoing stall - see watchdog_state.json (local,
gitignored, distinct from stale_lines_local.py's own heartbeat file).
Also alerts on recovery, so a stall-then-fix isn't silent.
"""

import json
import os
import subprocess
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

import stale_lines as sl

STALL_THRESHOLD_SECONDS = 300
HEARTBEAT_PATH = os.path.join("data", "stale_lines", "local_heartbeat.json")
WATCHDOG_STATE_PATH = os.path.join("data", "stale_lines", "watchdog_state.json")
RUNNER_TASK_NAME = "MLB Stale Lines Local"


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _attempt_restart():
    """Best-effort: Task Scheduler's own restart-on-failure only fires
    when the task's process actually EXITS - a genuinely hung-but-still-
    running process needs to be ended first. Returns a short status
    string for the alert; never raises."""
    try:
        subprocess.run(["schtasks", "/End", "/TN", RUNNER_TASK_NAME], capture_output=True, text=True, timeout=30)
        result = subprocess.run(["schtasks", "/Run", "/TN", RUNNER_TASK_NAME], capture_output=True, text=True, timeout=30)
        return "restart attempted" if result.returncode == 0 else f"restart attempt failed: {result.stderr.strip()}"
    except Exception as e:
        return f"restart attempt failed: {e}"


def main():
    now = datetime.now(timezone.utc)
    heartbeat = _load_json(HEARTBEAT_PATH, None)
    watchdog_state = _load_json(WATCHDOG_STATE_PATH, {"already_alerted": False})

    if heartbeat is None:
        age_seconds = None
        stalled = True
        reason = "no heartbeat file found - stale_lines_local may never have started"
    else:
        last_poll = datetime.fromisoformat(heartbeat["last_poll_utc"])
        age_seconds = (now - last_poll).total_seconds()
        stalled = age_seconds > STALL_THRESHOLD_SECONDS
        reason = f"last successful heartbeat was {age_seconds / 60:.1f} min ago"

    if stalled and not watchdog_state.get("already_alerted"):
        restart_status = _attempt_restart()
        sl.post_system_alert("⚠️ stale_lines_local appears stalled", f"{reason}. {restart_status}.")
        watchdog_state["already_alerted"] = True
        _save_json(WATCHDOG_STATE_PATH, watchdog_state)
        print(f"ALERTED: {reason}. {restart_status}.")
    elif not stalled and watchdog_state.get("already_alerted"):
        sl.post_system_alert("✅ stale_lines_local recovered", f"Heartbeat resumed - last poll {age_seconds:.0f}s ago.", color=0x2ECC71)
        watchdog_state["already_alerted"] = False
        _save_json(WATCHDOG_STATE_PATH, watchdog_state)
        print("RECOVERED: heartbeat resumed, cleared alert state.")
    else:
        print(f"OK: {reason}.")


if __name__ == "__main__":
    main()
