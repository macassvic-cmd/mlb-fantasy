"""
Soccer Start History - the soccer equivalent of start_history.py, for the
same reason: we need real "how often does this player actually start"
data to score DNS, and no such dataset exists yet for soccer. Built from
scrapers/espn_soccer.py's post-match squad data (get_match_squad_names),
the only real, working, non-paywalled source of confirmed starter/bench
status this repo has for soccer (ESPN's soccer injuries endpoint is
confirmed empty - see that module's docstring - which is why
Transfermarkt exists for injuries and this exists separately for history).

Schema: data/player_soccer_start_history.json:
  {espn_athlete_key: {"name":, "matches": [{"date","league","event_id",
   "team_id","opponent_team_id","started":bool,"active":bool}]}}
espn_athlete_key is normalize_name(fullName) - ESPN's roster entries in
the summary endpoint used here don't reliably carry a stable athlete id
in every league, so normalized name (same normalize_name() as the rest of
this codebase's soccer path) is the join key, same limitation
transfermarkt.py/soccer_dns.py already live with.

Usage:
  python soccer_start_history.py --backfill --league EPL --start 2026-08-01 --end 2026-09-13
  python soccer_start_history.py --record-date 2026-09-13 --league EPL
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta

from scrapers.espn_soccer import get_scoreboard, get_match_squad_names, normalize_name

HISTORY_PATH = os.path.join("data", "player_soccer_start_history.json")
BACKFILL_SLEEP_SECONDS = 0.3


def _load(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def _save(path, data):
    """Atomic write (temp file + os.replace) - found live 2026-09-14
    running a long backfill concurrently with a live scoring pass: a
    plain open-and-write left a real window where a concurrent reader
    (soccer_adapter.py's context build, which polls throughout the day)
    could see a truncated/partial JSON file mid-save and fail to load
    history entirely for that cycle. backfill() saves after every single
    date specifically so a killed/crashed run doesn't lose progress -
    that's exactly the pattern that needs this to be atomic, not a rare
    edge case worth ignoring.

    Retries os.replace on Windows's WinError 5 ("Access is denied") -
    confirmed live the SAME backfill run this fix was written for then
    crashed entirely on it: another process (a concurrent reader, or AV/
    backup software transiently opening the file) can hold it just long
    enough that a same-instant replace fails - a real, if rare, race,
    not a fundamental problem with the target file. A few short retries
    resolves it without giving up the atomicity guarantee (never a
    partial-write fallback)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())

    last_error = None
    for attempt in range(5):
        try:
            os.replace(tmp_path, path)
            return
        except OSError as e:
            last_error = e
            time.sleep(0.2 * (attempt + 1))
    raise last_error


def record_date(league_code, date_str, history=None):
    """Record every completed match's squad for one league/date. Idempotent -
    replaces (not duplicates) this date's entries for any player touched."""
    own_history = history is None
    if history is None:
        history = _load(HISTORY_PATH, {})

    events = get_scoreboard(league_code, date_str)
    recorded = 0
    for ev in events:
        event_id = ev.get("id")
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors", [])
        team_ids = {c.get("homeAway"): c.get("team", {}).get("id") for c in competitors}
        if not event_id:
            continue

        squad = get_match_squad_names(league_code, event_id)
        time.sleep(BACKFILL_SLEEP_SECONDS)
        if not squad:
            continue  # not played yet / postponed / ESPN hasn't posted it - not "nobody played"

        for norm_name, info in squad.items():
            team_id = info.get("team_id")
            is_home = team_id == team_ids.get("home")
            opp_team_id = team_ids.get("away") if is_home else team_ids.get("home")
            entry = history.setdefault(norm_name, {"name": norm_name, "matches": []})
            entry["matches"] = [m for m in entry["matches"] if m["event_id"] != event_id]
            entry["matches"].append({
                "date": date_str, "league": league_code, "event_id": event_id,
                "team_id": team_id, "opponent_team_id": opp_team_id,
                # "competition" is a synonym for "league" here (ESPN's
                # league_code IS the competition for this module's
                # domestic-league-only coverage - no separate cup/UCL
                # scraping exists yet) - kept as its own key anyway so a
                # caller/report can read "competition" without knowing
                # that's the same value as "league" today.
                "competition": league_code,
                "home_away": "home" if is_home else "away",
                "started": info["starter"], "active": info["active"],
            })
            recorded += 1

    for p in history.values():
        p["matches"].sort(key=lambda m: m["date"])

    print(f"soccer_start_history: recorded {recorded} player-matches for {league_code} {date_str}.")
    if own_history:
        _save(HISTORY_PATH, history)
    return history


def backfill(league_code, start_date, end_date):
    history = _load(HISTORY_PATH, {})
    d = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    dates = []
    while d <= end:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    print(f"soccer_start_history backfill: {league_code}, {len(dates)} dates, {start_date} .. {end_date}")
    for i, date_str in enumerate(dates, 1):
        print(f"[{i}/{len(dates)}] {league_code} {date_str}")
        try:
            history = record_date(league_code, date_str, history=history)
        except Exception as e:
            print(f"  ERROR: {e}")
        _save(HISTORY_PATH, history)

    print(f"Backfill complete -> {HISTORY_PATH} ({len(history)} players)")


# ---------------------------------------------------------------------------
# Query helpers - used by soccer_dns_score.py
# ---------------------------------------------------------------------------

def _name_variant_candidates(normalized_name):
    """Same fallback rotowire_soccer.py uses (2026-09-14 item 3) applied
    here for the same underlying reason: Dabble's player_name is a full
    LEGAL name ("Bryan Zaragoza Martinez") but ESPN's squad list - this
    module's only data source - indexes the common name ("Bryan
    Zaragoza"). Confirmed live: 101/140 LaLiga board players had zero
    history purely from this mismatch, not an actual backfill gap.

    A 4-word legal name ("Jose Luis Gaya Pena" = 2 given names + paternal
    + maternal surname) needs a 3rd variant beyond the 3-word case's
    first+last/first+second: ESPN's common name there is [given1]
    [paternal] = words[0]+words[2] ("Jose Gaya"), confirmed live the same
    way - not covered by either of the 3-word variants.

    Exact-match only against history's real keys, never fuzzy - a
    diminutive (Alejandro/Alex) or nickname (Jonathan Castro Otto/Jony)
    is a real, separate gap this deliberately does not try to guess."""
    words = normalized_name.split()
    if len(words) < 3:
        return []
    candidates = [f"{words[0]} {words[-1]}", f"{words[0]} {words[1]}"]
    if len(words) >= 4:
        candidates.append(f"{words[0]} {words[2]}")
    return candidates


def _resolve_key(history, normalized_name):
    if normalized_name in history:
        return normalized_name
    for variant in _name_variant_candidates(normalized_name):
        if variant in history:
            return variant
    return normalized_name


def _player_matches(history, normalized_name, before_date=None):
    p = history.get(_resolve_key(history, normalized_name))
    if not p:
        return []
    matches = p["matches"]
    if before_date is not None:
        matches = [m for m in matches if m["date"] < before_date]
    return sorted(matches, key=lambda m: m["date"])


def starts_last_n(history, normalized_name, n=5, before_date=None):
    matches = _player_matches(history, normalized_name, before_date)[-n:]
    started = sum(1 for m in matches if m["started"])
    return started, len(matches) - started, len(matches)


# Named n=3/5/10 wrappers (coverage audit item 4) - same starts_last_n
# underneath, just the exact call shape a caller/report can name directly
# without repeating the n= kwarg everywhere.
def starts_last_3(history, normalized_name, before_date=None):
    return starts_last_n(history, normalized_name, n=3, before_date=before_date)


def starts_last_5(history, normalized_name, before_date=None):
    return starts_last_n(history, normalized_name, n=5, before_date=before_date)


def starts_last_10(history, normalized_name, before_date=None):
    return starts_last_n(history, normalized_name, n=10, before_date=before_date)


# MINUTES-PLAYED DATA IS NOT AVAILABLE (checked live 2026-09-14): ESPN's
# soccer match-summary roster stats (the only per-player, per-match data
# source this module has - see module docstring) carries appearances/
# cards/goals/shots/saves per player but NO minutes-played field of any
# kind. minutes_last_3/5/10 were requested (coverage audit item 4) but
# cannot be honestly computed without a new data source - documented gap,
# not a silent omission or a fabricated value. If a real per-match minutes
# source is ever added, it belongs in record_date() alongside "started"/
# "active" - the query helpers below (start_rate/bench_rate) are already
# shaped to make adding "minutes_rate"-style helpers trivial then.
MINUTES_DATA_AVAILABLE = False


def start_rate(history, normalized_name, n=10, before_date=None):
    """Percent (0-100, rounded) of the last n recorded matches started,
    or None if there's no history at all - never divides by zero."""
    wins, losses, total = starts_last_n(history, normalized_name, n=n, before_date=before_date)
    return round(100 * wins / total, 1) if total else None


def bench_rate(history, normalized_name, n=10, before_date=None):
    """100 - start_rate - kept as its own named function (rather than
    making every caller compute 100-start_rate itself) since "bench rate"
    is the framing coverage reports/diagnostics actually want to show."""
    rate = start_rate(history, normalized_name, n=n, before_date=before_date)
    return round(100 - rate, 1) if rate is not None else None


def days_rest(history, normalized_name, as_of_date, before_date=None):
    """Days since this player's last match appearance (started or sub) -
    None if no history at all."""
    matches = _player_matches(history, normalized_name, before_date)
    appeared = [m for m in matches if m["active"]]
    if not appeared:
        return None
    last_date = datetime.strptime(appeared[-1]["date"], "%Y-%m-%d")
    return (datetime.strptime(as_of_date, "%Y-%m-%d") - last_date).days


def previous_start(history, normalized_name, before_date=None):
    """True/False/None - did this player START his most recent recorded
    match (None if there's no history at all). A simple, explicitly-
    named field (2026-09-14 item 2) distinct from first_recent_start
    (which asks a narrower "did he JUST break into the XI" question) -
    this is just "what happened last time," the raw building block a
    caller might want directly rather than only via the derived signals
    above."""
    matches = _player_matches(history, normalized_name, before_date)
    if not matches:
        return None
    return matches[-1]["started"]


def first_recent_start(history, normalized_name, n=5, before_date=None):
    """True if this player's most recent match was a start, but none of
    the n-1 matches before that were - i.e. exactly the Mama Balde case
    ("made his first start of the season last match")."""
    matches = _player_matches(history, normalized_name, before_date)[-n:]
    if not matches or not matches[-1]["started"]:
        return False
    return not any(m["started"] for m in matches[:-1])


def frequent_substitute(history, normalized_name, n=10, before_date=None):
    """True if this player regularly appears (bench-to-pitch) but rarely
    starts - a real rotation signal distinct from "injured"/"out"."""
    matches = _player_matches(history, normalized_name, before_date)[-n:]
    appeared = sum(1 for m in matches if m["active"])
    started = sum(1 for m in matches if m["started"])
    return appeared >= 5 and started <= appeared * 0.3


def rotation_player(history, normalized_name, n=10, before_date=None):
    """True if starts are inconsistent (neither a clear starter nor a
    clear non-starter) - the general "could go either way" signal."""
    matches = _player_matches(history, normalized_name, before_date)[-n:]
    if len(matches) < 5:
        return False
    rate = sum(1 for m in matches if m["started"]) / len(matches)
    return 0.3 <= rate <= 0.7


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # see soccer_dns.main's comment
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Soccer player start/bench history backfill")
    parser.add_argument("--league", required=True, help="league code, e.g. EPL/LLG/L1F/BUN/SEA/MLS")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--start", help="backfill start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="backfill end date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--record-date", default=None, help="record a single date YYYY-MM-DD")
    args = parser.parse_args()

    if args.backfill:
        if not args.start:
            parser.error("--backfill requires --start")
        end = args.end or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        backfill(args.league, args.start, end)
    elif args.record_date:
        record_date(args.league, args.record_date)
    else:
        parser.error("specify --backfill (with --start/--end) or --record-date")


if __name__ == "__main__":
    main()
