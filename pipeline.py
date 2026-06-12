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
from datetime import datetime, timedelta

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

def process_player(player_id, player_info, lineup_data, date_str):
    from scrapers.mlb_api import (
        get_player_season_stats, get_player_rolling_stats,
        get_pitcher_season_stats, get_player_info, get_days_rest,
    )
    from scrapers.statcast import get_batter_statcast_summary
    from scrapers.fangraphs import get_batter_fg_stats, get_pitcher_fg_stats, get_platoon_splits
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

    # Platoon splits
    try:
        record["platoon"] = get_platoon_splits(
            name,
            player_info.get("bat_side", "R"),
            matchup.get("pitcher_hand", "R"),
        )
    except Exception:
        record["platoon"] = {}

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

def run_pipeline(date_str):
    from scrapers.mlb_api import get_lineups, get_games, get_player_info
    from scrapers.lineups import get_rotowire_lineups

    logger.info(f"=== Pipeline start: {date_str} ===")
    os.makedirs("data", exist_ok=True)

    # Check for existing output
    out_path = os.path.join("data", f"{date_str}.json")

    # MLB API lineups
    logger.info("Fetching MLB schedule...")
    try:
        mlb_lineups = get_lineups(date_str)
    except Exception as e:
        logger.error(f"get_lineups failed: {e}")
        mlb_lineups = {}

    if not mlb_lineups:
        games = get_games(date_str)
        if not games:
            logger.warning(f"No games found for {date_str}")
            return []
        logger.warning(
            f"{len(games)} games found but lineups not yet posted. "
            "Re-run closer to game time (lineups typically post 2–3 hrs before first pitch)."
        )
        return []

    logger.info(f"Found {len(mlb_lineups)} batters in MLB lineups")

    # RotoWire confirmation
    logger.info("Fetching RotoWire lineups...")
    try:
        rw = get_rotowire_lineups()
    except Exception as e:
        logger.warning(f"RotoWire failed: {e}")
        rw = {}

    # Process each player
    all_players = []
    ids = list(mlb_lineups.keys())
    logger.info(f"Processing {len(ids)} players...")

    for i, pid in enumerate(ids, 1):
        lineup_data = mlb_lineups[pid]
        try:
            pinfo = get_player_info(pid)
        except Exception as e:
            logger.debug(f"player_info {pid}: {e}")
            pinfo = {"name": f"Player_{pid}", "position": "?", "bat_side": "R"}

        name = pinfo.get("name", "")
        logger.info(f"[{i}/{len(ids)}] {name}")

        try:
            rec = process_player(pid, pinfo, lineup_data, date_str)
            if rec is not None:
                rec["lineup_confirmed"] = name in rw
                all_players.append(rec)
        except Exception as e:
            logger.error(f"process_player failed for {name} ({pid}): {e}")

        time.sleep(0.15)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_players, f, indent=2, default=str)

    logger.info(f"Saved {len(all_players)} records to {out_path}")
    return all_players


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="MLB Fantasy Data Pipeline")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="Target date YYYY-MM-DD (default: today)")
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
