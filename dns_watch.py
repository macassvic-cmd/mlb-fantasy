"""
Watched-folder runner for the JSON-driven DNS detectors - the
replacement for Betr's API (now locked behind real HTTP Basic Auth,
see data/stale_lines/DISABLED) as the source of "what props are
currently live." Drop a JSON file into data/dns_watch/incoming/mlb/ or
.../soccer/ and it gets picked up, cross-referenced (Transfermarkt
injuries for soccer, MLB lineups + the return-date-vs-fixture filter
for baseball - completely unchanged detection logic, see board_loader.py's
module docstring), and posts to Discord exactly like the old Betr-
sourced runs did - individual flag cards plus the compiled digest.
Processed files move to data/dns_watch/processed/{sport}/; a file that
fails to parse or process moves to data/dns_watch/failed/{sport}/ with
a sibling .error.txt explaining why, rather than looping on it forever
or silently dropping it.

Book-agnostic by construction: nothing here or in board_loader.py knows
or cares which book the JSON came from - Betr, Dabble, a spreadsheet
export, anything - as long as the 5 required fields are present (see
board_loader.py's schema docs).

TWO MODES, pick based on how hands-off you want this:
  --once (default)  process whatever's sitting in incoming/ right now,
                     then exit. No persistent process, so none of the
                     orphan-accumulation risk this project has hit
                     twice already with long-lived local pollers (see
                     stale_lines_local.py's own history) - safe to run
                     by hand after dropping a file, or from a Scheduled
                     Task on a short interval.
  --watch            persistent polling loop (checks every --interval
                     seconds) for true "drop and forget." Carries that
                     same orphan risk if you forget it's running -
                     --once is the safer default for exactly that
                     reason.
"""

import argparse
import os
import shutil
import traceback
from datetime import datetime, timezone

BASE_DIR = os.path.join("data", "dns_watch")
INCOMING_DIR = os.path.join(BASE_DIR, "incoming")
PROCESSED_DIR = os.path.join(BASE_DIR, "processed")
FAILED_DIR = os.path.join(BASE_DIR, "failed")
SPORTS = ["mlb", "soccer"]
DEFAULT_WATCH_INTERVAL_SECONDS = 60


def _ensure_dirs():
    for sport in SPORTS:
        os.makedirs(os.path.join(INCOMING_DIR, sport), exist_ok=True)
        os.makedirs(os.path.join(PROCESSED_DIR, sport), exist_ok=True)
        os.makedirs(os.path.join(FAILED_DIR, sport), exist_ok=True)


def process_file(path, sport):
    from board_loader import load_prop_records, to_mlb_betr_entries, to_soccer_board

    records = load_prop_records(path, sport_hint=sport)

    if sport == "mlb":
        import stale_lines as sl
        entries = to_mlb_betr_entries(records)
        if not entries:
            raise ValueError(f"No 'mlb' records found in {path} (sport_hint was {sport!r}).")
        return sl.run_poll(betr_entries=entries)

    if sport == "soccer":
        import soccer_dns as sd
        board = to_soccer_board(records)
        if not board:
            raise ValueError(f"No 'soccer' records found in {path} (sport_hint was {sport!r}).")
        return sd.run_scan(board=board)

    raise ValueError(f"Unknown sport {sport!r} - expected one of {SPORTS}.")


def process_all_pending():
    _ensure_dirs()
    results = []
    for sport in SPORTS:
        in_dir = os.path.join(INCOMING_DIR, sport)
        for fname in sorted(os.listdir(in_dir)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(in_dir, fname)
            try:
                summary = process_file(path, sport)
                dest = os.path.join(PROCESSED_DIR, sport, fname)
                shutil.move(path, dest)
                print(f"[{sport}] processed {fname} -> {dest}\n  {summary}")
                results.append({"file": fname, "sport": sport, "status": "ok", "summary": summary})
            except Exception as e:
                dest = os.path.join(FAILED_DIR, sport, fname)
                shutil.move(path, dest)
                error_path = dest + ".error.txt"
                with open(error_path, "w", encoding="utf-8") as f:
                    f.write(f"{datetime.now(timezone.utc).isoformat()}\n{e}\n\n{traceback.format_exc()}")
                print(f"[{sport}] FAILED {fname}: {e}\n  (moved to {dest}, details in {error_path})")
                results.append({"file": fname, "sport": sport, "status": "failed", "error": str(e)})
    if not results:
        print("Nothing pending in data/dns_watch/incoming/{mlb,soccer}/.")
    return results


def watch_forever(interval):
    import time
    print(f"Watching {INCOMING_DIR}/{{mlb,soccer}} every {interval}s - Ctrl+C to stop.")
    while True:
        process_all_pending()
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", action="store_true",
                         help="Persistent polling loop instead of a single pass (see module docstring for the tradeoff).")
    parser.add_argument("--interval", type=int, default=DEFAULT_WATCH_INTERVAL_SECONDS,
                         help=f"Polling interval in seconds for --watch mode (default {DEFAULT_WATCH_INTERVAL_SECONDS}).")
    args = parser.parse_args()

    if args.watch:
        watch_forever(args.interval)
    else:
        process_all_pending()


if __name__ == "__main__":
    main()
