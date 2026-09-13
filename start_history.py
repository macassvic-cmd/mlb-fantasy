"""
Player Start History - the persistent dataset behind the DNP score (dnp_score.py).

data/{date}.json (pipeline.py's daily output) only ever contains the 9
confirmed/projected starters per team - a benched player is invisible there,
so "how often does X actually start" can't be reconstructed from existing
cached files. This module instead pulls real start/bench/entered-as-sub
history straight from MLB's per-game live-feed endpoint
(scrapers.mlb_api.get_game_start_data), which gives an authoritative answer
for any completed game, historical or otherwise.

Usage:
  python start_history.py --backfill --start 2026-07-30 --end 2026-09-12
  python start_history.py --date 2026-09-12   # record a single date
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta

from scrapers.mlb_api import get_games, get_game_start_data

HISTORY_PATH = os.path.join("data", "player_start_history.json")

# Politeness delay between per-game live-feed calls during a backfill -
# matches the sleep pattern already used elsewhere in this codebase
# (pipeline.py's 0.15s between players, backtest.py's 0.5s between dates)
# rather than hammering MLB's API back-to-back for a few thousand games.
BACKFILL_SLEEP_SECONDS = 0.2


def _load(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def _save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _date_already_recorded(history, date_str):
    """True if every player who has an entry for this date already has it -
    used to make backfill resumable without a separate progress file."""
    for p in history.values():
        if any(g["date"] == date_str for g in p.get("games", [])):
            return True
    return False


def record_date(date_str, history=None):
    """Pull every completed game on date_str and upsert each position
    player's start/bench outcome into the history dict. Idempotent - safe
    to call again for a date already recorded (existing entries for that
    date are replaced, not duplicated), same "recompute this date" pattern
    tracker.py's grade_* functions use."""
    own_history = history is None
    if history is None:
        history = _load(HISTORY_PATH, {})

    games = [g for g in get_games(date_str) if g.get("status", {}).get("abstractGameState") == "Final"]
    if not games:
        print(f"start_history: no Final games for {date_str} - skipping.")
        return history

    recorded = 0
    for game in games:
        game_pk = game.get("gamePk")
        team_names = {
            "home": (game.get("teams", {}).get("home", {}).get("team", {}) or {}).get("name"),
            "away": (game.get("teams", {}).get("away", {}).get("team", {}) or {}).get("name"),
        }
        data = get_game_start_data(game_pk)
        time.sleep(BACKFILL_SLEEP_SECONDS)
        if data is None:
            print(f"  {date_str} game {game_pk}: fetch failed - skipping this game.")
            continue

        for side in ("home", "away"):
            opp_side = "away" if side == "home" else "home"
            side_data = data[side]
            for p in side_data["players"]:
                pid = p.get("player_id")
                if pid is None:
                    continue
                pid = str(pid)
                entry = history.setdefault(pid, {"name": p.get("name"), "games": []})
                entry["name"] = p.get("name")
                entry["games"] = [g for g in entry["games"] if g["date"] != date_str]
                entry["games"].append({
                    "date": date_str,
                    "game_pk": game_pk,
                    "team": team_names[side],
                    "opp_team": team_names[opp_side],
                    "started": p["started"],
                    "batting_order": p["batting_order_slot"],
                    "entered_as_sub": p["entered_as_sub"],
                    "opp_pitcher_id": side_data["opp_pitcher_id"],
                    "opp_pitcher_hand": side_data["opp_pitcher_hand"],
                    "position": p["position"],
                })
                recorded += 1

    for p in history.values():
        p["games"].sort(key=lambda g: g["date"])

    print(f"start_history: recorded {recorded} player-games across {len(games)} games for {date_str}.")
    if own_history:
        _save(HISTORY_PATH, history)
    return history


def backfill(start_date, end_date):
    history = _load(HISTORY_PATH, {})
    d = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    dates = []
    while d <= end:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)

    print(f"start_history backfill: {len(dates)} dates, {start_date} .. {end_date}")
    for i, date_str in enumerate(dates, 1):
        if _date_already_recorded(history, date_str):
            print(f"[{i}/{len(dates)}] {date_str}: already recorded - skipping (resumable backfill).")
            continue
        print(f"[{i}/{len(dates)}] {date_str}")
        try:
            history = record_date(date_str, history=history)
        except Exception as e:
            print(f"  ERROR: {e}")
        _save(HISTORY_PATH, history)  # save after every date so a crash loses at most one date

    print(f"Backfill complete -> {HISTORY_PATH} ({len(history)} players)")


# ---------------------------------------------------------------------------
# Query helpers - used by dnp_score.py
# ---------------------------------------------------------------------------

def _player_games(history, player_id, before_date=None):
    p = history.get(str(player_id))
    if not p:
        return []
    games = p["games"]
    if before_date is not None:
        games = [g for g in games if g["date"] < before_date]
    return sorted(games, key=lambda g: g["date"])


def start_rate(history, player_id, n_games=10, before_date=None):
    """(wins, losses, n) where "win" = started, over the last n_games this
    player's team played (not n_games this player started - a player who
    was benched should have that count against them, not be skipped)."""
    games = _player_games(history, player_id, before_date)[-n_games:]
    started = sum(1 for g in games if g["started"])
    return started, len(games) - started, len(games)


def start_rate_vs_hand(history, player_id, hand, n_games=15, before_date=None):
    games = [g for g in _player_games(history, player_id, before_date) if g.get("opp_pitcher_hand") == hand]
    games = games[-n_games:]
    started = sum(1 for g in games if g["started"])
    return started, len(games) - started, len(games)


def consecutive_starts(history, player_id, before_date=None):
    """How many of this player's most recent CONSECUTIVE games (in the
    history we have) were starts, counting back from the end until the
    first non-start."""
    games = _player_games(history, player_id, before_date)
    streak = 0
    for g in reversed(games):
        if not g["started"]:
            break
        streak += 1
    return streak


def games_since_last_start(history, player_id, before_date=None):
    """None if they've never started in the recorded history, else how many
    of their team's games (in our data) have passed since their last start."""
    games = _player_games(history, player_id, before_date)
    for i, g in enumerate(reversed(games)):
        if g["started"]:
            return i
    return None if not games else len(games)


def main():
    parser = argparse.ArgumentParser(description="Player start/bench history backfill")
    parser.add_argument("--backfill", action="store_true", help="backfill a date range")
    parser.add_argument("--start", help="backfill start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="backfill end date YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--date", default=None, help="record a single date YYYY-MM-DD")
    args = parser.parse_args()

    if args.backfill:
        if not args.start:
            parser.error("--backfill requires --start")
        end = args.end or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        backfill(args.start, end)
    elif args.date:
        record_date(args.date)
    else:
        parser.error("specify --backfill (with --start/--end) or --date")


if __name__ == "__main__":
    main()
