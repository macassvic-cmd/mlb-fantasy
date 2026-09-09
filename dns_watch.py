"""
Watched-folder runner for the JSON-driven DNS detectors - the
replacement for Betr's API (now locked behind real HTTP Basic Auth,
see data/stale_lines/DISABLED) as the source of "what props are
currently live." Drop a JSON file into data/dns_watch/incoming/{sport}/
(mlb, soccer, nfl, nba, nhl - see SPORTS) and it gets picked up, cross-
referenced against the sport-appropriate real-world data (MLB lineups +
the return-date-vs-fixture filter, Transfermarkt injuries for soccer,
ESPN injury reports for nfl/nba/nhl - see board_loader.py's and
pro_league_dns.py's module docstrings), and posts to Discord exactly
like the old Betr-sourced runs did - individual flag cards plus the
compiled digest. Processed files move to data/dns_watch/processed/
{sport}/; a file that fails to parse or process moves to
data/dns_watch/failed/{sport}/ with a sibling .error.txt explaining why,
rather than looping on it forever or silently dropping it.

nfl/nba/nhl (added 2026-09-09) don't have grading yet - unlike mlb/
soccer, they only alert, they don't yet track win/loss - see
pro_league_dns.py's own docstring for why that's deferred rather than
built on an unverified guess. CFB (college football) and cricket/rugby
league/tennis/esports are NOT supported - CFB was checked and has no
reliable injury-report source (college programs aren't required to
disclose the way pro leagues are); the others haven't been researched.

Book-agnostic by construction: nothing here or in board_loader.py knows
or cares which book the JSON came from - Betr, Dabble, a spreadsheet
export, anything - as long as the 5 required fields are present (see
board_loader.py's schema docs). Both process_file() and
process_mixed_file() load with skip_invalid=True - an individual
malformed record (missing field, empty string where a real value was
expected) is skipped and reported in the run summary rather than
aborting the whole file, since a large automated export inevitably has
a few bad rows and one shouldn't cost every other valid record in it.

MIXED-SPORT FILES: if your export bundles every sport into one file
instead of one file per sport, drop it directly in
data/dns_watch/incoming/ (the root, NOT a sport sub-folder). Every
record must carry its own "sport" field (or alias "league_sport") -
since there's no folder name to fall back on for a default.
process_mixed_file() splits the records by that field and routes each
supported sport's subset through _run_sport_detector() (shared with
process_file(), so the two never drift on how a sport gets dispatched),
each exactly as if it had been the whole file, and quietly ignores any
record whose "sport" isn't one of SPORTS (reported in the summary, not
treated as an error - a bundled export covering sports we don't support
yet shouldn't fail the whole file). Files placed in a sport's own sub-
folder are still assumed single-sport and processed the old way - use
the sub-folders when you already know a file is one sport, the root
when it's bundled.

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
# nfl/nba/nhl added 2026-09-09 - injury-report based (pro_league_dns.py),
# no grading yet (see that module's docstring for why). CFB/cricket/
# rugby league/tennis/esports were checked and are NOT included here -
# either no reliable data source was found (CFB) or none has been
# researched yet (the others) - see this session's own findings.
SPORTS = ["mlb", "soccer", "nfl", "nba", "nhl"]
PRO_LEAGUES = ["nfl", "nba", "nhl"]
MIXED = "mixed"  # bucket name for processed/failed sub-folders of root-level (multi-sport) files
DEFAULT_WATCH_INTERVAL_SECONDS = 60


def _ensure_dirs():
    os.makedirs(INCOMING_DIR, exist_ok=True)
    for sport in SPORTS:
        os.makedirs(os.path.join(INCOMING_DIR, sport), exist_ok=True)
    for sport in SPORTS + [MIXED]:
        os.makedirs(os.path.join(PROCESSED_DIR, sport), exist_ok=True)
        os.makedirs(os.path.join(FAILED_DIR, sport), exist_ok=True)


def _run_sport_detector(sport, records):
    """Runs the sport-appropriate detector on already-loaded, already-
    filtered-to-one-sport records, returning its summary dict. Shared by
    process_file() (single-sport folder) and process_mixed_file()
    (bundled export) so the two never drift apart on how a sport gets
    dispatched."""
    from board_loader import to_mlb_betr_entries, to_pro_league_entries, to_soccer_board

    if sport == "mlb":
        import stale_lines as sl
        entries, skipped_pitchers = to_mlb_betr_entries(records, report_skipped=True)
        summary = sl.run_poll(betr_entries=entries)
        if skipped_pitchers:
            summary = {"skipped_pitchers": {"count": len(skipped_pitchers), "names": [p["name"] for p in skipped_pitchers]}, **summary}
        return summary

    if sport == "soccer":
        import soccer_dns as sd
        return sd.run_scan(board=to_soccer_board(records))

    if sport in PRO_LEAGUES:
        import pro_league_dns as pld
        return pld.run_scan(to_pro_league_entries(records, sport), league=sport)

    raise ValueError(f"Unknown sport {sport!r} - expected one of {SPORTS}.")


def process_file(path, sport):
    from board_loader import load_prop_records

    # skip_invalid=True: a large automated export can have a handful of
    # genuinely bad rows (see load_prop_records' own docstring) - one bad
    # row shouldn't sink every other valid record in the file. Skipped
    # rows are still reported (skipped_invalid_records below), not lost.
    records, skipped = load_prop_records(path, sport_hint=sport, skip_invalid=True)
    if not records:
        raise ValueError(f"No usable {sport!r} records in {path} (sport_hint was {sport!r}) - "
                          f"{len(skipped)} record(s) were invalid." if skipped else
                          f"No records found in {path} (sport_hint was {sport!r}).")

    summary = _run_sport_detector(sport, records)
    if skipped:
        summary = {"skipped_invalid_records": {"count": len(skipped), "examples": skipped[:5]}, **summary}
    return summary


def process_mixed_file(path):
    """For a file dropped in incoming/ (root) rather than a sport
    sub-folder - every record must carry its own "sport" (no folder to
    default to), so this loads with sport_hint=None, splits by that
    field, and runs each recognized sport's records through its own
    detector, just as if that sport had been the whole file. Records
    whose "sport" isn't one of SPORTS are ignored, not errored - only
    raises if NOTHING recognizable was found at all."""
    from board_loader import load_prop_records

    records, skipped = load_prop_records(path, sport_hint=None, skip_invalid=True)

    by_sport = {}
    for r in records:
        by_sport.setdefault(r.sport, []).append(r)

    unsupported = {s: len(rs) for s, rs in by_sport.items() if s not in SPORTS}
    summary = {"skipped_unsupported_sports": unsupported}
    if skipped:
        summary["skipped_invalid_records"] = {"count": len(skipped), "examples": skipped[:5]}

    any_supported = False
    for sport in SPORTS:
        sport_records = by_sport.get(sport, [])
        if not sport_records:
            continue
        any_supported = True
        summary[sport] = _run_sport_detector(sport, sport_records)

    if not any_supported:
        raise ValueError(f"No supported-sport records found in {path} - "
                          f"sports present were {dict((s, len(rs)) for s, rs in by_sport.items())!r}.")

    return summary


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

    for fname in sorted(os.listdir(INCOMING_DIR)):
        path = os.path.join(INCOMING_DIR, fname)
        if not fname.endswith(".json") or not os.path.isfile(path):
            continue  # skips the mlb/, soccer/ sub-folders themselves
        try:
            summary = process_mixed_file(path)
            dest = os.path.join(PROCESSED_DIR, MIXED, fname)
            shutil.move(path, dest)
            print(f"[{MIXED}] processed {fname} -> {dest}\n  {summary}")
            results.append({"file": fname, "sport": MIXED, "status": "ok", "summary": summary})
        except Exception as e:
            dest = os.path.join(FAILED_DIR, MIXED, fname)
            shutil.move(path, dest)
            error_path = dest + ".error.txt"
            with open(error_path, "w", encoding="utf-8") as f:
                f.write(f"{datetime.now(timezone.utc).isoformat()}\n{e}\n\n{traceback.format_exc()}")
            print(f"[{MIXED}] FAILED {fname}: {e}\n  (moved to {dest}, details in {error_path})")
            results.append({"file": fname, "sport": MIXED, "status": "failed", "error": str(e)})

    if not results:
        print("Nothing pending in data/dns_watch/incoming/ (root or {mlb,soccer}/).")
    return results


def watch_forever(interval):
    import time
    print(f"Watching {INCOMING_DIR}/ (root + {{{','.join(SPORTS)}}}/) every {interval}s - Ctrl+C to stop.")
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
