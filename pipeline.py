"""
MLB Fantasy Baseball Data Pipeline
Usage:
  python pipeline.py                    # today's games
  python pipeline.py --date 2025-06-05  # specific date
  python pipeline.py --backfill 7       # last 7 days
"""

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("pipeline.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency version guard
# ---------------------------------------------------------------------------
# pandas 3.0.3 is a confirmed segfault trigger deep in native code (see the
# pin comment in requirements.txt, and the 2026-07-18/19 pipeline incidents).
# CI installs from requirements.txt fresh every run and can't drift, but a
# local machine's globally-installed packages can silently be on an
# untested version - this refuses to run instead of segfaulting mid-slate.
_VERSION_PINS = {
    "pandas": ((2, 0), (3, 0)),
    "numpy": ((2, 0), (3, 0)),
    "pyarrow": ((25, 0), (26, 0)),
}


def _check_pinned_versions():
    problems = []
    for name, (lo, hi) in _VERSION_PINS.items():
        try:
            mod = __import__(name)
            ver = tuple(int(p) for p in mod.__version__.split(".")[:2])
        except Exception:
            continue  # not installed - let the real import below fail with a clearer error
        if not (lo <= ver < hi):
            problems.append((name, mod.__version__, lo, hi))

    if problems:
        print("ERROR: installed package versions fall outside the pins in requirements.txt.")
        print("pandas 3.0.3 in particular is a confirmed segfault trigger - refusing to run.")
        for name, ver, lo, hi in problems:
            print(f"  {name} {ver} installed, need >={lo[0]}.{lo[1]},<{hi[0]}.{hi[1]}")
        print()
        print("Fix:")
        print('  python -m pip install "pandas>=2.0,<3.0" "numpy>=2.0,<3.0" "pyarrow>=25.0,<26.0"')
        sys.exit(1)


_check_pinned_versions()

# ---------------------------------------------------------------------------
# Park factors: (HR index, runs index, hits index) relative to neutral = 1.00
# ---------------------------------------------------------------------------
PARK_FACTORS = {
    "Coors Field":                 {"hr": 1.20, "runs": 1.25, "hits": 1.12},
    "Globe Life Field":            {"hr": 1.15, "runs": 1.10, "hits": 1.05},
    "Great American Ball Park":    {"hr": 1.12, "runs": 1.08, "hits": 1.04},
    "Yankee Stadium":              {"hr": 1.12, "runs": 1.05, "hits": 1.01},
    "Oriole Park at Camden Yards": {"hr": 1.10, "runs": 1.05, "hits": 1.03},
    "Citizens Bank Park":          {"hr": 1.08, "runs": 1.06, "hits": 1.03},
    "Guaranteed Rate Field":       {"hr": 1.08, "runs": 1.03, "hits": 1.01},
    "Chase Field":                 {"hr": 1.05, "runs": 1.03, "hits": 1.02},
    "Rogers Centre":               {"hr": 1.05, "runs": 1.03, "hits": 1.02},
    "American Family Field":       {"hr": 1.06, "runs": 1.03, "hits": 1.02},
    "Wrigley Field":               {"hr": 1.05, "runs": 1.03, "hits": 1.02},
    "Fenway Park":                 {"hr": 0.95, "runs": 1.05, "hits": 1.10},
    "Minute Maid Park":            {"hr": 1.03, "runs": 1.02, "hits": 1.01},
    "Truist Park":                 {"hr": 1.03, "runs": 1.02, "hits": 1.01},
    "Angel Stadium":               {"hr": 1.02, "runs": 1.01, "hits": 1.00},
    "Progressive Field":           {"hr": 1.00, "runs": 1.00, "hits": 1.00},
    "Nationals Park":              {"hr": 1.01, "runs": 1.00, "hits": 1.00},
    "loanDepot park":              {"hr": 0.96, "runs": 0.97, "hits": 0.98},
    "Citi Field":                  {"hr": 0.95, "runs": 0.97, "hits": 0.97},
    "Kauffman Stadium":            {"hr": 0.95, "runs": 0.97, "hits": 0.98},
    "Fenway Park":                 {"hr": 0.95, "runs": 1.05, "hits": 1.10},
    "Tropicana Field":             {"hr": 0.92, "runs": 0.95, "hits": 0.96},
    "Busch Stadium":               {"hr": 0.93, "runs": 0.95, "hits": 0.96},
    "Comerica Park":               {"hr": 0.93, "runs": 0.96, "hits": 0.97},
    "PNC Park":                    {"hr": 0.97, "runs": 0.97, "hits": 0.99},
    "Dodger Stadium":              {"hr": 0.97, "runs": 0.97, "hits": 0.98},
    "Target Field":                {"hr": 0.98, "runs": 0.97, "hits": 0.98},
    "T-Mobile Park":               {"hr": 0.90, "runs": 0.93, "hits": 0.95},
    "Petco Park":                  {"hr": 0.88, "runs": 0.92, "hits": 0.95},
    "Oracle Park":                 {"hr": 0.80, "runs": 0.90, "hits": 0.92},
    "Oakland Coliseum":            {"hr": 0.88, "runs": 0.90, "hits": 0.92},
}
_DEFAULT_PF = {"hr": 1.00, "runs": 1.00, "hits": 1.00}


def park_factor(venue_name):
    if venue_name in PARK_FACTORS:
        return PARK_FACTORS[venue_name]
    for k, v in PARK_FACTORS.items():
        if venue_name and (venue_name.lower() in k.lower() or k.lower() in venue_name.lower()):
            return v
    return _DEFAULT_PF


# ---------------------------------------------------------------------------
# Per-player projection
# ---------------------------------------------------------------------------

# Raw per-game rates derived from rolling-window stats can run hot (e.g. an
# 11-for-24 week works out to ~0.9 singles/game). Each field is linearly
# compressed from an observed raw range into a realistic per-game band
# before park/matchup/weather adjustments are applied.
# Format: field -> (raw_lo, raw_hi, target_lo, target_hi)
PROJECTION_RANGES = {
    "singles": (0.20, 1.20, 0.15, 0.45),
    "doubles": (0.05, 0.35, 0.03, 0.12),
    "triples": (0.00, 0.08, 0.00, 0.015),
    "hr":      (0.00, 0.50, 0.03, 0.12),
    "bb":      (0.10, 0.70, 0.06, 0.18),
    "hbp":     (0.00, 0.12, 0.01, 0.04),
    "rbi":     (0.20, 1.40, 0.10, 0.55),
    "runs":    (0.20, 1.20, 0.10, 0.55),
    "sb":      (0.00, 0.40, 0.02, 0.15),
}


def _compress(value, raw_lo, raw_hi, target_lo, target_hi):
    frac = (value - raw_lo) / (raw_hi - raw_lo)
    frac = min(max(frac, 0.0), 1.0)
    return target_lo + frac * (target_hi - target_lo)


def calculate_projection(record):
    """
    Produce per-game projected counting stats adjusted for park / weather / matchup.
    We pick the shortest reliable rolling window (prefer 14d, fall back to 30d, then season).
    """
    def per_game(stats, games, field):
        return (stats.get(field) or 0) / games if games else 0

    r14 = record.get("rolling_14d", {})
    r30 = record.get("rolling_30d", {})
    sea = record.get("season_stats", {})

    g14 = r14.get("games", 0) or 0
    g30 = r30.get("games", 0) or 0
    g_sea = sea.get("games", 0) or 0

    if g14 >= 5:
        base, g = r14, g14
    elif g30 >= 10:
        base, g = r30, g30
    elif g_sea >= 10:
        base, g = sea, g_sea
    else:
        return {}

    proj = {f: per_game(base, g, f) for f in ("singles", "doubles", "triples", "hr", "bb", "hbp", "rbi", "runs", "sb")}

    # Compress raw per-game rates into realistic ranges
    for f, (raw_lo, raw_hi, tgt_lo, tgt_hi) in PROJECTION_RANGES.items():
        proj[f] = _compress(proj[f], raw_lo, raw_hi, tgt_lo, tgt_hi)

    # Park factor
    pf = record.get("park_factor", _DEFAULT_PF)
    proj["hr"] *= pf.get("hr", 1.0)
    for f in ("singles", "doubles", "triples"):
        proj[f] *= pf.get("hits", 1.0)

    # Matchup: scale by opp ERA vs league average (4.20)
    matchup = record.get("matchup", {})
    era = matchup.get("era") or matchup.get("era_fg")
    if era and era < 15:
        era_scale = float(era) / 4.20
        for f in proj:
            proj[f] *= era_scale

    # Hot weather HR boost for outdoor parks
    wx = record.get("weather", {})
    if not wx.get("is_indoor") and wx.get("temp_f", 72) > 82:
        proj["hr"] *= 1.05

    # Final clamp to realistic per-game ranges
    for f, (_, _, tgt_lo, tgt_hi) in PROJECTION_RANGES.items():
        proj[f] = min(max(proj[f], tgt_lo), tgt_hi)

    return {k: round(v, 4) for k, v in proj.items()}


# ---------------------------------------------------------------------------
# Single-player processing
# ---------------------------------------------------------------------------

def process_player(player_id, player_info, lineup_data, date_str, platoon_data=None):
    from scrapers.mlb_api import (
        get_player_season_stats, get_player_rolling_stats,
        get_pitcher_season_stats, get_player_info, get_days_rest,
    )
    from scrapers.statcast import get_batter_statcast_summary
    from scrapers.fangraphs import get_batter_fg_stats, get_pitcher_fg_stats, match_platoon_matchup
    from scrapers.weather import get_stadium_weather

    pos = player_info.get("position", "")
    # Skip pitchers batting (NL / two-way edge cases handled downstream)
    if pos == "P":
        return None

    opp_pitcher = lineup_data.get("opponent_pitcher") or {}
    opp_pitcher_id = opp_pitcher.get("id")
    opp_pitcher_name = opp_pitcher.get("fullName", "")

    record = {
        "player_id": player_id,
        "name": player_info.get("name", f"ID_{player_id}"),
        "position": pos,
        "bat_side": player_info.get("bat_side", "R"),
        "team_id": lineup_data.get("team_id"),
        "team_name": lineup_data.get("team_name", ""),
        "opp_team_name": lineup_data.get("opp_team_name", ""),
        "batting_order": lineup_data.get("batting_order"),
        "home_away": lineup_data.get("home_away"),
        "venue_id": lineup_data.get("venue_id"),
        "venue_name": lineup_data.get("venue_name", ""),
        "game_pk": lineup_data.get("game_pk"),
        "game_date_utc": lineup_data.get("game_date_utc"),
        "date": date_str,
        "opp_pitcher_name": opp_pitcher_name,
    }

    # Season stats
    try:
        record["season_stats"] = get_player_season_stats(player_id)
    except Exception as e:
        logger.debug(f"season_stats {player_id}: {e}")
        record["season_stats"] = {}

    # Rolling windows
    for days in (7, 14, 30):
        try:
            record[f"rolling_{days}d"] = get_player_rolling_stats(player_id, days=days, end_date=date_str)
        except Exception as e:
            logger.debug(f"rolling_{days}d {player_id}: {e}")
            record[f"rolling_{days}d"] = {}
        time.sleep(0.05)

    # Statcast
    try:
        record["statcast"] = get_batter_statcast_summary(player_id, date_str)
    except Exception as e:
        logger.debug(f"statcast {player_id}: {e}")
        record["statcast"] = {}

    # FanGraphs
    name = player_info.get("name", "")
    try:
        record["fg_stats"] = get_batter_fg_stats(name)
    except Exception as e:
        logger.debug(f"fg_stats {name}: {e}")
        record["fg_stats"] = {}

    # Opponent pitcher
    matchup = {}
    if opp_pitcher_id:
        try:
            matchup.update(get_pitcher_season_stats(opp_pitcher_id))
        except Exception as e:
            logger.debug(f"pitcher mlb {opp_pitcher_id}: {e}")
        if opp_pitcher_name:
            try:
                matchup.update(get_pitcher_fg_stats(opp_pitcher_name))
            except Exception as e:
                logger.debug(f"pitcher fg {opp_pitcher_name}: {e}")
        try:
            pit_info = get_player_info(opp_pitcher_id)
            matchup["pitcher_hand"] = pit_info.get("pitch_hand", "R")
        except Exception:
            matchup["pitcher_hand"] = "R"
    matchup["pitcher_name"] = opp_pitcher_name
    record["matchup"] = matchup

    # Platoon splits - real vs-LHP/vs-RHP (batter) and vs-LHB/vs-RHB
    # (pitcher) splits matched against today's actual matchup handedness.
    # Normally precomputed in parallel by run_pipeline() (see
    # _fetch_platoon_map) and passed in via platoon_data; only falls back to
    # a synchronous fetch here when process_player() is called standalone.
    if platoon_data is not None:
        record["platoon"] = platoon_data
    else:
        try:
            record["platoon"] = match_platoon_matchup(
                player_id,
                player_info.get("bat_side", "R"),
                opp_pitcher_id,
                matchup.get("pitcher_hand", "R"),
            )
        except Exception as e:
            logger.debug(f"platoon matchup {player_id}: {e}")
            record["platoon"] = {}
        time.sleep(0.5)  # be polite to Baseball-Reference - no caching on their split pages

    # Park factor
    record["park_factor"] = park_factor(record["venue_name"])

    # Weather
    try:
        record["weather"] = get_stadium_weather(record["venue_name"])
    except Exception as e:
        logger.debug(f"weather {record['venue_name']}: {e}")
        record["weather"] = {}

    # Days rest
    try:
        record["days_rest"] = get_days_rest(player_id, date_str)
    except Exception:
        record["days_rest"] = None

    # Projected per-game stats
    record["projected"] = calculate_projection(record)

    return record


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------

def _fill_missing_lineups(mlb_lineups, games, date_str):
    """Ensure every team in today's full schedule has at least a lineup
    entry, confirmed where MLB has posted one, otherwise projected from
    that team's most recent confirmed batting order. Early used to skip a
    game ENTIRELY if its lineup wasn't posted by 9am - since MLB posts
    lineups per-team (often hours apart, sometimes only one side of a
    game by the time Early runs), that silently dropped every late-posting
    game and lost the CLV window on it. This fills in only what's actually
    missing, per team, so Early always captures the full slate; whichever
    side already has a confirmed lineup is left untouched."""
    from scrapers.mlb_api import get_recent_team_lineup

    fetched_at = datetime.now(timezone.utc).isoformat()
    for v in mlb_lineups.values():
        v.setdefault("lineup_status", "confirmed")
        v.setdefault("lineup_source", "mlb_official")
        v.setdefault("lineup_source_updated_at", fetched_at)
        v.setdefault("lineup_fetched_at", fetched_at)

    covered_teams = {(v["game_pk"], v["team_id"]) for v in mlb_lineups.values()}

    for game in games:
        game_pk = game["gamePk"]
        home = game["teams"]["home"]
        away = game["teams"]["away"]
        venue = game.get("venue", {})
        home_pitcher = home.get("probablePitcher", {})
        away_pitcher = away.get("probablePitcher", {})

        def fallback(team, opp_team, opp_pitcher, side):
            team_id = team["team"]["id"]
            if (game_pk, team_id) in covered_teams:
                return
            player_ids = get_recent_team_lineup(team_id, date_str)
            if not player_ids:
                logger.warning(
                    f"No recent lineup available to project for "
                    f"{team['team']['name']} (game {game_pk}) - skipping that side"
                )
                return
            for i, pid in enumerate(player_ids):
                mlb_lineups[pid] = {
                    "game_pk": game_pk,
                    "team_id": team_id,
                    "team_name": team["team"]["name"],
                    "opp_team_id": opp_team["team"]["id"],
                    "opp_team_name": opp_team["team"]["name"],
                    "opponent_pitcher": opp_pitcher,
                    "batting_order": i + 1,
                    "home_away": side,
                    "venue_id": venue.get("id"),
                    "venue_name": venue.get("name", ""),
                    "game_date_utc": game.get("gameDate"),
                    "lineup_status": "projected",
                    "lineup_source": "mlb_recent_game_projection",
                    "lineup_source_updated_at": None,  # no real "posted at" for a projection - honest gap
                    "lineup_fetched_at": fetched_at,
                }

        fallback(home, away, away_pitcher, "home")
        fallback(away, home, home_pitcher, "away")

    return mlb_lineups


def _protect_against_lineup_regression(date_str, mlb_lineups):
    """(mlb_lineups, carried_over_records) - a stale projected record must
    never override a newer confirmed lineup, applied here in the
    direction that actually matters day to day: a NEWER fetch that
    (transiently) came back empty/incomplete for a game must never
    overwrite an OLDER but confirmed read from an earlier run the same
    date. MLB does not "unpost" a posted lineup, so a previously-
    confirmed player showing up as projected-or-missing on a later
    same-day run is far more likely a transient fetch gap than a real
    change - guarded here rather than trusted blindly.

    carried_over_records are COMPLETE prior process_player() output
    records (not re-run through processing this cycle - their stats/
    projections are last-run's, only their lineup confirmation status
    is what's being protected) for any guarded player; that player is
    also REMOVED from the returned mlb_lineups so the main loop doesn't
    reprocess them against this run's degraded lineup_data (whose shape
    doesn't match a finished record anyway). Every disagreement is
    logged, never silently resolved, and this only ever RESTORES a
    prior confirmed record - it can't fabricate a new one."""
    out_path = os.path.join("data", f"{date_str}.json")
    if not os.path.exists(out_path):
        return mlb_lineups, []
    try:
        with open(out_path, encoding="utf-8") as f:
            previous = json.load(f)
    except Exception:
        return mlb_lineups, []

    previously_confirmed = {
        p["player_id"]: p for p in previous
        if p.get("lineup_status") == "confirmed" and p.get("player_id") is not None
    }

    carried_over = []
    for pid, prev in previously_confirmed.items():
        current = mlb_lineups.get(pid)
        if current is not None and current.get("lineup_status") == "confirmed":
            continue  # still confirmed this run (possibly a reordered lineup) - trust the fresh read
        logger.warning(
            f"lineup regression guarded: player_id={pid} ({prev.get('team_name')}, game_pk="
            f"{prev.get('game_pk')}) was confirmed as of {prev.get('lineup_source_updated_at')} but this "
            f"run's fetch shows {'projected' if current else 'no lineup at all'} - keeping the prior "
            f"confirmed record instead of downgrading it (its stats/projections are last run's, not "
            f"reprocessed this cycle)."
        )
        mlb_lineups.pop(pid, None)
        carried_over.append(dict(prev, lineup_source=prev.get("lineup_source", "mlb_official") + "_retained"))

    if carried_over:
        logger.warning(f"lineup regression guard retained {len(carried_over)} previously-confirmed player(s) this run.")
    return mlb_lineups, carried_over


def _fetch_platoon_map(mlb_lineups, pinfo_map, pitcher_hand_map, max_workers=8):
    """Pull every batter's Baseball-Reference platoon-split matchup
    concurrently instead of one at a time - each lookup is an independent
    network call (and request volume is the bottleneck, not CPU), so a
    small thread pool cuts wall-clock time roughly proportional to worker
    count without changing what data each player ends up with."""
    from scrapers.fangraphs import match_platoon_matchup

    args_by_pid = {}
    for pid, ldata in mlb_lineups.items():
        pinfo = pinfo_map.get(pid) or {}
        opp_pitcher = ldata.get("opponent_pitcher") or {}
        opp_pitcher_id = opp_pitcher.get("id")
        args_by_pid[pid] = (
            pid,
            pinfo.get("bat_side", "R"),
            opp_pitcher_id,
            pitcher_hand_map.get(opp_pitcher_id, "R"),
        )

    platoon_map = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_pid = {
            pool.submit(match_platoon_matchup, *args): pid
            for pid, args in args_by_pid.items()
        }
        for future in as_completed(future_to_pid):
            pid = future_to_pid[future]
            try:
                platoon_map[pid] = future.result()
            except Exception as e:
                logger.debug(f"platoon matchup {pid}: {e}")
                platoon_map[pid] = {}

    return platoon_map


def print_lineup_audit(date_str, all_players, games):
    """Prints the per-team lineup-confirmation summary required before
    publishing (2026-09-14 lineup-confirmation bug fix) - makes a
    regressed/stale confirmation impossible to miss silently. A team
    counts as "confirmed" only if EVERY one of its batters in today's
    data is confirmed; any team with even one non-confirmed batter
    (or the lineup-regression guard's carried-over record) is listed
    under "projected" so a partial/mixed state is never hidden inside
    a rounded-up "confirmed" count. "unavailable" = scheduled today but
    with zero player records at all - a total fetch failure for that
    whole side, distinct from "posted late" (projected)."""
    team_status = {}
    for p in all_players:
        team = p.get("team_name")
        if not team:
            continue
        team_status.setdefault(team, set()).add(p.get("lineup_status", "confirmed"))

    scheduled_teams = set()
    for g in games:
        scheduled_teams.add(g["teams"]["home"]["team"]["name"])
        scheduled_teams.add(g["teams"]["away"]["team"]["name"])

    confirmed_teams = sorted(t for t, statuses in team_status.items() if statuses == {"confirmed"})
    projected_teams = sorted(t for t, statuses in team_status.items() if statuses != {"confirmed"})
    unavailable_teams = sorted(scheduled_teams - set(team_status.keys()))

    print("\n" + "=" * 70)
    print(f"LINEUP AUDIT - {date_str}")
    print("=" * 70)
    print(f"Games today: {len(games)}")
    print(f"Teams with confirmed lineup: {len(confirmed_teams)}")
    print(f"Teams still projected: {len(projected_teams)}")
    print(f"Teams unavailable (no lineup data at all): {len(unavailable_teams)}")
    if projected_teams:
        print("\nTeams still marked PROJECTED:")
        for t in projected_teams:
            print(f"  - {t}")
    if unavailable_teams:
        print("\nTeams UNAVAILABLE (no data fetched):")
        for t in unavailable_teams:
            print(f"  - {t}")
    print("=" * 70 + "\n")

    return {"games": len(games), "confirmed_teams": confirmed_teams,
            "projected_teams": projected_teams, "unavailable_teams": unavailable_teams}


def run_pipeline(date_str):
    from scrapers.mlb_api import get_lineups, get_games, get_player_info

    logger.info(f"=== Pipeline start: {date_str} ===")
    os.makedirs("data", exist_ok=True)

    # Check for existing output
    out_path = os.path.join("data", f"{date_str}.json")

    # MLB API lineups
    logger.info("Fetching MLB schedule...")
    games = get_games(date_str)
    if not games:
        logger.warning(f"No games found for {date_str}")
        return []

    try:
        mlb_lineups = get_lineups(date_str)
    except Exception as e:
        logger.error(f"get_lineups failed: {e}")
        mlb_lineups = {}

    confirmed_count = len(mlb_lineups)
    mlb_lineups = _fill_missing_lineups(mlb_lineups, games, date_str)
    mlb_lineups, carried_over_records = _protect_against_lineup_regression(date_str, mlb_lineups)
    projected_count = sum(1 for v in mlb_lineups.values() if v.get("lineup_status") != "confirmed")

    if not mlb_lineups and not carried_over_records:
        logger.warning("Could not build any lineups (confirmed or projected) - skipping.")
        return []

    if projected_count:
        logger.warning(
            f"{projected_count} batters had no posted lineup yet - "
            f"projected from each team's most recent confirmed batting order "
            f"so no game in today's {len(games)}-game slate gets dropped."
        )
    logger.info(
        f"Found {len(mlb_lineups)} batters across {len(games)} games "
        f"({confirmed_count} confirmed, {projected_count} projected)"
    )

    def _resolve_player_info(pid):
        try:
            return get_player_info(pid)
        except Exception as e:
            logger.warning(f"player_info {pid} failed ({e}), retrying without hydration")
            try:
                from scrapers.mlb_api import _get
                data = _get(f"/people/{pid}", {})
                people = data.get("people", [])
                if people:
                    p = people[0]
                    return {
                        "name": p.get("fullName", f"Player_{pid}"),
                        "bat_side": p.get("batSide", {}).get("code", "R"),
                        "pitch_hand": p.get("pitchHand", {}).get("code", "R"),
                        "position": p.get("primaryPosition", {}).get("abbreviation", "?"),
                        "team_id": None,
                    }
                return {"name": f"Player_{pid}", "position": "?", "bat_side": "R"}
            except Exception as e2:
                logger.debug(f"player_info fallback {pid}: {e2}")
                return {"name": f"Player_{pid}", "position": "?", "bat_side": "R"}

    # Process each player
    all_players = []
    ids = list(mlb_lineups.keys())
    logger.info(f"Processing {len(ids)} players...")

    # Resolve every batter's info up front so the platoon-split lookups
    # below have what they need (bat_side, opposing pitcher hand) to run
    # as a single parallel batch instead of being interleaved one-by-one
    # inside the per-player loop.
    pinfo_map = {pid: _resolve_player_info(pid) for pid in ids}

    pitcher_hand_map = {}
    distinct_pitcher_ids = {
        (mlb_lineups[pid].get("opponent_pitcher") or {}).get("id") for pid in ids
    }
    distinct_pitcher_ids.discard(None)
    for opp_pid in distinct_pitcher_ids:
        try:
            pitcher_hand_map[opp_pid] = get_player_info(opp_pid).get("pitch_hand", "R")
        except Exception as e:
            logger.debug(f"pitcher_hand {opp_pid}: {e}")
            pitcher_hand_map[opp_pid] = "R"

    logger.info(
        f"Fetching platoon splits for {len(ids)} batters "
        f"({len(distinct_pitcher_ids)} opposing pitchers) in parallel..."
    )
    platoon_map = _fetch_platoon_map(mlb_lineups, pinfo_map, pitcher_hand_map)

    for i, pid in enumerate(ids, 1):
        lineup_data = mlb_lineups[pid]
        pinfo = pinfo_map[pid]
        name = pinfo.get("name", "")
        logger.info(f"[{i}/{len(ids)}] {name}")

        try:
            rec = process_player(pid, pinfo, lineup_data, date_str, platoon_data=platoon_map.get(pid))
            if rec is not None:
                lineup_status = lineup_data.get("lineup_status", "confirmed")
                rec["lineup_status"] = lineup_status
                rec["lineup_confirmed"] = lineup_status == "confirmed"
                rec["lineup_source"] = lineup_data.get("lineup_source")
                rec["lineup_source_updated_at"] = lineup_data.get("lineup_source_updated_at")
                rec["lineup_fetched_at"] = lineup_data.get("lineup_fetched_at")
                all_players.append(rec)
        except Exception as e:
            logger.error(f"process_player failed for {name} ({pid}): {e}")

        time.sleep(0.15)

    if carried_over_records:
        all_players.extend(carried_over_records)
        logger.info(f"Carried forward {len(carried_over_records)} previously-confirmed player record(s) "
                    f"that this run's fetch couldn't reproduce (see lineup regression guard above).")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_players, f, indent=2, default=str)

    logger.info(f"Saved {len(all_players)} records to {out_path}")

    print_lineup_audit(date_str, all_players, games)

    # MLB DNP/DNS DISCONTINUED (2026-09-14) - focus moved 100% to Soccer
    # DNS. dnp_score.py/dabble_adapter.py/dnp_alerts.py are left on disk
    # (do not delete - historical data/results stay readable) but are no
    # longer invoked from production. Re-enable by restoring the block
    # below (see git history for this exact diff) if MLB DNS is ever
    # revived.
    # try:
    #     import dnp_score
    #     dnp_score.score_today(date_str)
    # except Exception as e:
    #     logger.warning(f"dnp_score.score_today failed (non-fatal): {e}")

    # Freshness marker, added 2026-09-04: a same-day rerun (common during
    # the dense CI retry schedule - see .github/workflows/pipeline.yml's
    # CRON STRATEGY) very often produces BYTE-IDENTICAL output to an
    # earlier fetch that same day, since most of the underlying source
    # data (season/rolling stats, matchups) hasn't changed yet if the
    # slate's games haven't started - `git diff --cached --quiet` then
    # correctly finds nothing to commit, so data/{date}.json's last-commit
    # timestamp never advances. The CI freshness gate used to read THAT
    # timestamp via `git log`, which meant it never saw a fetch as
    # "just happened" once the content stopped changing - every fire past
    # the 3h mark re-triggered a full ~15min redundant fetch forever,
    # exactly backwards from the "redundant fires are nearly free" design
    # the dense schedule depends on (confirmed live 2026-09-04: 5 of 6
    # fires that day did a full wasted refetch). This file's CONTENT
    # changes every single successful run regardless of whether the data
    # itself did, so git always commits it and the CI freshness check can
    # read fetched_at_utc directly instead of inferring "did we just fetch"
    # from a git history side effect that doesn't reliably track it.
    marker_path = os.path.join("data", ".pipeline_last_fetch.json")
    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                   "player_count": len(all_players)}, f, indent=2)

    return all_players


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    from scrapers.mlb_api import mlb_today_str

    parser = argparse.ArgumentParser(description="MLB Fantasy Data Pipeline")
    parser.add_argument("--date", default=mlb_today_str(),
                        help="Target date YYYY-MM-DD (default: today, Pacific-anchored - see "
                             "scrapers.mlb_api.mlb_today_str)")
    parser.add_argument("--backfill", type=int, default=0,
                        help="Also process N previous days (0 = today only)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    base = datetime.strptime(args.date, "%Y-%m-%d")
    dates = [
        (base - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(args.backfill, -1, -1)
    ]

    for d in dates:
        players = run_pipeline(d)
        if players:
            print(f"\nDone: {d}: {len(players)} players processed. Run projections.py to see rankings.\n")


if __name__ == "__main__":
    main()
