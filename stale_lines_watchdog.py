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


def _kill_all_runner_processes():
    """Find and force-kill EVERY python.exe whose command line references
    stale_lines_local.py, plus each one's process tree - not just whichever
    single process Task Scheduler happens to be tracking as "the task."

    Fixes a real bug found live 2026-09-02: the task launches via
    run_stale_lines_local.bat under cmd.exe (see setup_stale_lines_local_
    task.ps1), so the PID Task Scheduler tracks is cmd.exe, with
    python.exe as its CHILD. The old `schtasks /End` only signals that
    tracked cmd.exe PID - on Windows, ending a parent does not kill its
    child, so the actual python.exe runner survived, detached from Task
    Scheduler's tracking. Task Scheduler then considered the task "not
    running" and happily started ANOTHER instance on the next restart.
    Four such orphans had accumulated over 24h this way, all racing
    unlocked on the same state.json/events.jsonl/git repo. Enumerating by
    command line and killing every match (taskkill's /T tree-kills each
    one, /F forces it) closes the gap regardless of which process Task
    Scheduler itself thinks is "the" runner."""
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
            "| Where-Object { $_.CommandLine -like '*stale_lines_local.py*' } "
            "| Select-Object -ExpandProperty ProcessId"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
        )
        pids = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
    except Exception as e:
        return f"process lookup failed: {e}"

    if not pids:
        return "no runner processes found to kill"

    killed, failed = [], []
    for pid in pids:
        r = subprocess.run(["taskkill", "/F", "/T", "/PID", pid], capture_output=True, text=True, timeout=15)
        (killed if r.returncode == 0 else failed).append(pid)
    status = f"killed {len(killed)} process(es) {killed}"
    if failed:
        status += f", failed to kill {failed}"
    return status


def _attempt_restart():
    """Best-effort: kills EVERY orphaned copy of the runner - not just the
    one Task Scheduler happens to be tracking (see _kill_all_runner_
    processes for why that distinction matters) - then starts the task
    fresh. Returns a short status string for the alert; never raises."""
    try:
        kill_status = _kill_all_runner_processes()
        result = subprocess.run(["schtasks", "/Run", "/TN", RUNNER_TASK_NAME], capture_output=True, text=True, timeout=30)
        start_status = "task started" if result.returncode == 0 else f"task start failed: {result.stderr.strip()}"
        return f"{kill_status}; {start_status}"
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
