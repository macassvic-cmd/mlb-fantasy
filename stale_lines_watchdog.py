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

SECOND FAILURE MODE (added 2026-09-08): "stalled" only catches a HUNG
process (heartbeat stops updating). It does NOT catch a process that's
still polling on schedule but where every single poll's actual WORK is
failing - stale_lines_local.py's own write_heartbeat() runs in both the
try AND except branches of its poll loop (by design, so a heartbeat
reader can always see the latest attempt's outcome), which means the
heartbeat file stays fresh even when every poll is erroring. Confirmed
live: Betr's API started 401'ing on 2026-09-07 and ran for 2195+
consecutive failures (~18 hours) with `stalled` never once true, because
age_seconds never exceeded STALL_THRESHOLD_SECONDS - the watchdog was
checking the wrong field. Fixed by also reading heartbeat["ok"]
directly: a heartbeat that's fresh (not stalled) but persistently
ok=False for FAILING_CHECK_THRESHOLD consecutive watchdog runs (~10 min,
long enough to ignore one transient blip) triggers its own alert -
deliberately WITHOUT attempting a restart, since a dead external API
(the actual cause of that incident - Betr locking their GraphQL endpoint
behind real HTTP Basic Auth, not something a restart can fix) means
killing and relaunching the process would just accomplish nothing while
resetting state that might be useful for debugging.
"""

import json
import os
import subprocess
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

import stale_lines as sl

STALL_THRESHOLD_SECONDS = 300
# 2 consecutive failing watchdog checks (~10 min at the 5-min trigger
# interval) - long enough that one transient network blip doesn't fire
# a false alarm, short enough that this never again takes 18 hours to
# notice a sustained outage.
FAILING_CHECK_THRESHOLD = 2
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
    watchdog_state = _load_json(WATCHDOG_STATE_PATH, {
        "already_alerted": False,
        "already_alerted_failing": False,
        "consecutive_failing_checks": 0,
    })
    watchdog_state.setdefault("already_alerted_failing", False)
    watchdog_state.setdefault("consecutive_failing_checks", 0)

    if heartbeat is None:
        age_seconds = None
        stalled = True
        reason = "no heartbeat file found - stale_lines_local may never have started"
    else:
        last_poll = datetime.fromisoformat(heartbeat["last_poll_utc"])
        age_seconds = (now - last_poll).total_seconds()
        stalled = age_seconds > STALL_THRESHOLD_SECONDS
        reason = f"last successful heartbeat was {age_seconds / 60:.1f} min ago"

    # --- Failure mode 1: hung process (heartbeat stopped updating) -------
    if stalled and not watchdog_state.get("already_alerted"):
        restart_status = _attempt_restart()
        sl.post_system_alert("⚠️ stale_lines_local appears stalled", f"{reason}. {restart_status}.")
        watchdog_state["already_alerted"] = True
        _save_json(WATCHDOG_STATE_PATH, watchdog_state)
        print(f"ALERTED: {reason}. {restart_status}.")
        return
    elif not stalled and watchdog_state.get("already_alerted"):
        sl.post_system_alert("✅ stale_lines_local recovered", f"Heartbeat resumed - last poll {age_seconds:.0f}s ago.", color=0x2ECC71)
        watchdog_state["already_alerted"] = False
        _save_json(WATCHDOG_STATE_PATH, watchdog_state)
        print("RECOVERED: heartbeat resumed, cleared alert state.")
        return

    # --- Failure mode 2: alive and polling on schedule, but every poll's
    # actual work is failing (heartbeat fresh, ok=False) - see module
    # docstring for the 2026-09-07 incident this exists to catch. Only
    # reached when NOT stalled (mode 1 above already handled + returned
    # for that case). No restart attempted here - see docstring for why. -
    if heartbeat is not None and not heartbeat.get("ok", True):
        watchdog_state["consecutive_failing_checks"] += 1
        error = heartbeat.get("error", "(no error message recorded)")

        if (watchdog_state["consecutive_failing_checks"] >= FAILING_CHECK_THRESHOLD
                and not watchdog_state["already_alerted_failing"]):
            minutes_failing = watchdog_state["consecutive_failing_checks"] * 5  # approx, watchdog's own 5-min cadence
            sl.post_system_alert(
                "🟠 stale_lines_local is polling but every poll is failing",
                f"Heartbeat is fresh (process is alive and on schedule) but has read "
                f"ok=False for >= {minutes_failing} min. Latest error:\n```{error}```\n"
                f"NOT attempting a restart - a process-alive-but-failing pattern is "
                f"usually an external cause (dead/changed upstream API, revoked access) "
                f"that a restart won't fix. Needs a human to look at the actual error.",
            )
            watchdog_state["already_alerted_failing"] = True
            _save_json(WATCHDOG_STATE_PATH, watchdog_state)
            print(f"ALERTED (failing, not stalled): {error}")
            return

        _save_json(WATCHDOG_STATE_PATH, watchdog_state)
        print(f"OK (stalled=False) but heartbeat ok=False "
              f"({watchdog_state['consecutive_failing_checks']}/{FAILING_CHECK_THRESHOLD} checks): {error}")
        return

    # Healthy - clear the failing-streak counter/alert if it was set.
    if watchdog_state["consecutive_failing_checks"] or watchdog_state["already_alerted_failing"]:
        if watchdog_state["already_alerted_failing"]:
            sl.post_system_alert(
                "✅ stale_lines_local polls are succeeding again",
                f"Heartbeat now reads ok=True - last poll {age_seconds:.0f}s ago.",
                color=0x2ECC71,
            )
            print("RECOVERED: polls succeeding again, cleared failing-alert state.")
        watchdog_state["consecutive_failing_checks"] = 0
        watchdog_state["already_alerted_failing"] = False
        _save_json(WATCHDOG_STATE_PATH, watchdog_state)
        return

    print(f"OK: {reason}.")


if __name__ == "__main__":
    main()
