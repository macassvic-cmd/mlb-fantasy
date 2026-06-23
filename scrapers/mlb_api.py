"""
MLB Stats API scraper — free, no key required.
Provides schedule, lineups, player/pitcher stats, and game logs.
"""

import requests
import logging
from datetime import datetime, timedelta

from scrapers._timeout import call_with_timeout

BASE = "https://statsapi.mlb.com/api/v1"
logger = logging.getLogger(__name__)


def _get(path, params=None, timeout=20):
    # requests' own `timeout` only bounds connect/read after DNS resolves -
    # it doesn't always bound a hung DNS lookup. Wrap with a hard wall-clock
    # backstop so a single stuck call can never block the whole pipeline.
    resp = call_with_timeout(
        requests.get, f"{BASE}{path}", params=params, timeout=timeout,
        timeout_s=60, label=f"MLB API {path}",
    )
    if resp is None:
        raise RuntimeError(f"MLB API request timed out or failed: {path}")
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Schedule / Lineups
# ---------------------------------------------------------------------------

def get_schedule(date_str):
    return _get("/schedule", {
        "date": date_str,
        "sportId": 1,
        "hydrate": "lineups,probablePitcher,team,venue",
    })


def get_games(date_str):
    data = get_schedule(date_str)
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games


def get_recent_team_lineup(team_id, before_date, max_lookback=10):
    """Return the batting order (list of player_ids, in order) from the most
    recent game `team_id` played with a posted lineup before `before_date`.
    Used to project a lineup when today's hasn't been confirmed yet."""
    d = datetime.strptime(before_date, "%Y-%m-%d")
    for i in range(1, max_lookback + 1):
        check_date = (d - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            data = _get("/schedule", {
                "date": check_date, "sportId": 1, "teamId": team_id, "hydrate": "lineups",
            })
        except Exception as e:
            logger.debug(f"get_recent_team_lineup {team_id} {check_date}: {e}")
            continue

        for sd in data.get("dates", []):
            for game in sd.get("games", []):
                home = game["teams"]["home"]
                away = game["teams"]["away"]
                lineups_data = game.get("lineups", {})
                if home["team"]["id"] == team_id:
                    players = lineups_data.get("homePlayers", [])
                elif away["team"]["id"] == team_id:
                    players = lineups_data.get("awayPlayers", [])
                else:
                    continue
                if players:
                    return [p["id"] for p in players]
    return []


def get_lineups(date_str):
    """
    Returns {player_id: lineup_record} for every batter in today's lineups.
    Falls back gracefully if lineups aren't posted yet.
    """
    games = get_games(date_str)
    lineups = {}

    for game in games:
        game_pk = game["gamePk"]
        home = game["teams"]["home"]
        away = game["teams"]["away"]
        venue = game.get("venue", {})
        home_pitcher = home.get("probablePitcher", {})
        away_pitcher = away.get("probablePitcher", {})
        lineups_data = game.get("lineups", {})

        def register(players, team, opp_team, opp_pitcher, side):
            for i, p in enumerate(players):
                lineups[p["id"]] = {
                    "game_pk": game_pk,
                    "team_id": team["team"]["id"],
                    "team_name": team["team"]["name"],
                    "opp_team_id": opp_team["team"]["id"],
                    "opp_team_name": opp_team["team"]["name"],
                    "opponent_pitcher": opp_pitcher,
                    "batting_order": i + 1,
                    "home_away": side,
                    "venue_id": venue.get("id"),
                    "venue_name": venue.get("name", ""),
                    "game_date_utc": game.get("gameDate"),
                }

        register(lineups_data.get("homePlayers", []), home, away, away_pitcher, "home")
        register(lineups_data.get("awayPlayers", []), away, home, home_pitcher, "away")

    return lineups


# ---------------------------------------------------------------------------
# Player info
# ---------------------------------------------------------------------------

def get_player_info(player_id):
    data = _get(f"/people/{player_id}", {"hydrate": "currentTeam"})
    info = {}
    for p in data.get("people", []):
        info = {
            "name": p.get("fullName", ""),
            "bat_side": p.get("batSide", {}).get("code", "R"),
            "pitch_hand": p.get("pitchHand", {}).get("code", "R"),
            "position": p.get("primaryPosition", {}).get("abbreviation", ""),
            "team_id": p.get("currentTeam", {}).get("id"),
        }
    return info


# ---------------------------------------------------------------------------
# Hitting stats
# ---------------------------------------------------------------------------

def _parse_hitting(split_stat):
    s = split_stat
    hits = int(s.get("hits", 0) or 0)
    doubles = int(s.get("doubles", 0) or 0)
    triples = int(s.get("triples", 0) or 0)
    hr = int(s.get("homeRuns", 0) or 0)
    singles = max(0, hits - doubles - triples - hr)
    return {
        "avg": float(s.get("avg", 0) or 0),
        "obp": float(s.get("obp", 0) or 0),
        "slg": float(s.get("slg", 0) or 0),
        "hits": hits,
        "singles": singles,
        "doubles": doubles,
        "triples": triples,
        "hr": hr,
        "rbi": int(s.get("rbi", 0) or 0),
        "runs": int(s.get("runs", 0) or 0),
        "sb": int(s.get("stolenBases", 0) or 0),
        "bb": int(s.get("baseOnBalls", 0) or 0),
        "k": int(s.get("strikeOuts", 0) or 0),
        "hbp": int(s.get("hitByPitch", 0) or 0),
        "ab": int(s.get("atBats", 0) or 0),
        "games": int(s.get("gamesPlayed", 0) or 0),
    }


def _add_fantasy_pts(stats):
    g = stats.get("games", 0) or 0
    if g == 0:
        stats["ud_fpts_per_game"] = 0.0
        stats["pp_fpts_per_game"] = 0.0
        return stats

    ud = (stats["singles"] * 3 + stats["doubles"] * 6 + stats["triples"] * 8
          + stats["hr"] * 10 + stats["bb"] * 3 + stats["hbp"] * 3
          + stats["rbi"] * 2 + stats["runs"] * 2 + stats["sb"] * 4)
    pp = (stats["singles"] * 3 + stats["doubles"] * 5 + stats["triples"] * 8
          + stats["hr"] * 10 + stats["bb"] * 2 + stats["hbp"] * 2
          + stats["rbi"] * 2 + stats["runs"] * 2 + stats["sb"] * 5)
    stats["ud_fpts_per_game"] = round(ud / g, 2)
    stats["pp_fpts_per_game"] = round(pp / g, 2)
    return stats


def get_player_season_stats(player_id, season=None):
    if season is None:
        season = datetime.now().year
    data = _get(f"/people/{player_id}/stats", {
        "stats": "season", "group": "hitting", "season": season,
    })
    for sg in data.get("stats", []):
        for sp in sg.get("splits", []):
            return _add_fantasy_pts(_parse_hitting(sp.get("stat", {})))
    return {}


def get_player_game_log(player_id, date_str):
    """Return the parsed hitting stat line for a single date, or None if the
    player did not appear in a game that day."""
    data = _get(f"/people/{player_id}/stats", {
        "stats": "gameLog",
        "group": "hitting",
        "startDate": date_str,
        "endDate": date_str,
    })
    for sg in data.get("stats", []):
        for sp in sg.get("splits", []):
            if sp.get("date") == date_str:
                return _parse_hitting(sp.get("stat", {}))
    return None


def get_player_rolling_stats(player_id, days=14, end_date=None):
    if end_date is None:
        end_date = datetime.now()
    elif isinstance(end_date, str):
        end_date = datetime.strptime(end_date, "%Y-%m-%d")
    start = end_date - timedelta(days=days)
    data = _get(f"/people/{player_id}/stats", {
        "stats": "byDateRange",
        "group": "hitting",
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": (end_date - timedelta(days=1)).strftime("%Y-%m-%d"),
    })
    for sg in data.get("stats", []):
        for sp in sg.get("splits", []):
            return _add_fantasy_pts(_parse_hitting(sp.get("stat", {})))
    return {}


# ---------------------------------------------------------------------------
# Pitcher stats
# ---------------------------------------------------------------------------

def get_pitcher_season_stats(pitcher_id, season=None):
    if not pitcher_id:
        return {}
    if season is None:
        season = datetime.now().year
    data = _get(f"/people/{pitcher_id}/stats", {
        "stats": "season", "group": "pitching", "season": season,
    })
    for sg in data.get("stats", []):
        for sp in sg.get("splits", []):
            s = sp.get("stat", {})
            bf = int(s.get("battersFaced", 0) or 0)
            k = int(s.get("strikeOuts", 0) or 0)
            bb = int(s.get("baseOnBalls", 0) or 0)
            hr = int(s.get("homeRuns", 0) or 0)
            ip = float(s.get("inningsPitched", 0) or 0)
            era = float(s.get("era", 99) or 99)
            whip = float(s.get("whip", 2.0) or 2.0)
            k_pct = round(k / bf, 3) if bf > 0 else 0.0
            fip = round(((13 * hr + 3 * bb - 2 * k) / ip) + 3.10, 2) if ip > 0 else 5.00
            return {
                "era": era,
                "whip": whip,
                "k_pct": k_pct,
                "fip": fip,
                "ip": ip,
                "k9": float(s.get("strikeoutsPer9Inn", 0) or 0),
            }
    return {}


# ---------------------------------------------------------------------------
# Days rest
# ---------------------------------------------------------------------------

def get_days_rest(player_id, date_str):
    end = datetime.strptime(date_str, "%Y-%m-%d")
    start = end - timedelta(days=8)
    try:
        data = _get(f"/people/{player_id}/stats", {
            "stats": "gameLog",
            "group": "hitting",
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": (end - timedelta(days=1)).strftime("%Y-%m-%d"),
        })
        dates = []
        for sg in data.get("stats", []):
            for sp in sg.get("splits", []):
                d = sp.get("date")
                if d:
                    dates.append(d)
        if dates:
            last = sorted(dates)[-1]
            return (end - datetime.strptime(last, "%Y-%m-%d")).days
    except Exception as e:
        logger.debug(f"days_rest failed for {player_id}: {e}")
    return None
