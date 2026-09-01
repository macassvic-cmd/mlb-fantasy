"""
MLB Stats API scraper — free, no key required.
Provides schedule, lineups, player/pitcher stats, and game logs.
"""

import requests
import logging
from datetime import datetime, timedelta

from scrapers._timeout import call_with_timeout

BASE = "https://statsapi.mlb.com/api/v1"
BASE_V11 = "https://statsapi.mlb.com/api/v1.1"
logger = logging.getLogger(__name__)

# Shared Session, not a bare requests.get() per call - measured directly
# against statsapi.mlb.com: a fresh connection costs ~15s (TLS handshake/
# setup - DNS alone is ~0.5s, actual server response is ~0.1s once
# connected), while a REUSED connection on the same Session drops to
# ~0.1s. Irrelevant for a single GitHub Actions poll making one or two
# calls, but stale_lines.check_roster_status can make ~30 sequential
# calls (one per team with a Betr-lined player) in a single poll - at
# ~15s/call unpooled that's 7+ minutes, incompatible with a 30-second
# local polling cadence. With this Session reused across the whole
# poll, the same 30 calls cost one ~15s connection setup + ~29 x 0.1s.
# Safe as a module-level singleton here: every call to this module goes
# through call_with_timeout's one-thread-at-a-time model (a new thread
# per call, but the previous one has already joined before the next
# starts) - never true concurrent access, which is the actual case
# requests.Session's thread-safety caveat is about.
_session = requests.Session()


def _get(path, params=None, timeout=20):
    # requests' own `timeout` only bounds connect/read after DNS resolves -
    # it doesn't always bound a hung DNS lookup. Wrap with a hard wall-clock
    # backstop so a single stuck call can never block the whole pipeline.
    resp = call_with_timeout(
        _session.get, f"{BASE}{path}", params=params, timeout=timeout,
        timeout_s=60, label=f"MLB API {path}",
    )
    if resp is None:
        raise RuntimeError(f"MLB API request timed out or failed: {path}")
    resp.raise_for_status()
    return resp.json()


def _get_v11(path, params=None, timeout=20):
    resp = call_with_timeout(
        _session.get, f"{BASE_V11}{path}", params=params, timeout=timeout,
        timeout_s=60, label=f"MLB API v1.1 {path}",
    )
    if resp is None:
        raise RuntimeError(f"MLB API v1.1 request timed out or failed: {path}")
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


_team_abbr_cache = None


def get_team_abbreviations():
    """Return {abbreviation: team_id} for all active MLB teams, e.g.
    {"SF": 137, ...}. Cached for the process lifetime - used by
    stale_lines.py to resolve Betr's team.name (which IS the MLB
    abbreviation - confirmed empirically 2026-08-31) back to an MLB team,
    without needing a full roster fetch."""
    global _team_abbr_cache
    if _team_abbr_cache is None:
        data = _get("/teams", {"sportId": 1})
        _team_abbr_cache = {t["abbreviation"]: t["id"] for t in data.get("teams", []) if t.get("abbreviation")}
    return _team_abbr_cache


_roster_cache = {}


def get_active_roster(team_id):
    """Return {player_id: fullName} for team_id's current 26-man active
    roster - includes bench/benched players, not just today's starters.
    Cached per team_id for the process lifetime (a poll cycle looks this
    up once per team, not once per player). Used by stale_lines.py to
    resolve a Betr player name to an MLB numeric player_id (Betr's own
    player IDs are a different, unrelated ID space), since only a roster
    fetch - not the day's lineup - covers a scratched/benched player."""
    if team_id not in _roster_cache:
        data = _get(f"/teams/{team_id}/roster", {"rosterType": "active"})
        _roster_cache[team_id] = {p["person"]["id"]: p["person"]["fullName"] for p in data.get("roster", [])}
    return _roster_cache[team_id]


def get_live_feed_batting_orders(game_pk):
    """Return {"home": [player_id, ...] or None, "away": [...] or None} from
    the game's live feed (/api/v1.1/game/{game_pk}/feed/live) -
    liveData.boxscore.teams.{home,away}.battingOrder.

    This is a genuinely separate code path from get_lineups' schedule
    hydrate=lineups above - confirmed empirically 2026-08-31 that it
    populates pre-game (as soon as lineups are announced), not just once
    the game starts. Used by stale_lines.py as a general second-source
    cross-check against a schedule-hydration gap (source 1 says absent,
    source 2 says started -> trust source 2). NOTE: this would NOT have
    caught the one real miss found in the Phase 1 backtest (Fernando
    Tatis Jr., 2026-08-12) - on inspection both this endpoint and the
    schedule hydration agreed he wasn't a starter; he was a genuine
    late substitute (battingOrder "901"), not a data gap. Kept as
    defense against a genuine future hydration disagreement, which is a
    real (if less common) failure mode distinct from that case. None for
    a side means that side's battingOrder wasn't present at all (distinct
    from an empty list, which would mean the field existed but was empty
    - not observed in practice, but handled the same way: "can't confirm"
    rather than "confirmed absent")."""
    try:
        data = _get_v11(f"/game/{game_pk}/feed/live")
    except Exception as e:
        logger.warning(f"get_live_feed_batting_orders({game_pk}) failed: {e}")
        return {"home": None, "away": None}
    box = data.get("liveData", {}).get("boxscore", {}).get("teams", {})
    return {
        "home": box.get("home", {}).get("battingOrder") or None,
        "away": box.get("away", {}).get("battingOrder") or None,
    }


_forty_man_cache = {}


def get_forty_man_roster(team_id):
    """Return [{"player_id":, "name":, "status_code":, "status_desc":,
    "note":}] for team_id's 40-man roster - unlike get_active_roster
    (rosterType="active", 26 players), this INCLUDES IL/optioned players
    with their status (e.g. "D10"/"D15"/"D60" injured-list tiers, "RM"
    reassigned to minors) and an optional free-text `note` (often the
    injury detail, e.g. "Right rib stress fracture."). Cached per
    team_id for the process lifetime. Used by stale_lines.py's
    roster-status early-signal check: a player with a posted line whose
    CURRENT status isn't "A" (Active) is a high-confidence pre-lineup
    signal they won't be starting today."""
    if team_id not in _forty_man_cache:
        data = _get(f"/teams/{team_id}/roster", {"rosterType": "40Man"})
        _forty_man_cache[team_id] = [
            {
                "player_id": p["person"]["id"],
                "name": p["person"]["fullName"],
                "status_code": p.get("status", {}).get("code"),
                "status_desc": p.get("status", {}).get("description"),
                "note": p.get("note"),
            }
            for p in data.get("roster", [])
        ]
    return _forty_man_cache[team_id]


def clear_per_poll_caches():
    """Clear _roster_cache and _forty_man_cache (NOT _team_abbr_cache,
    which never changes intra-season and is safe to keep forever).

    Both are documented as "cached for the process lifetime" - true and
    harmless under GitHub Actions, where every poll IS a fresh process
    (`python stale_lines.py` exits after one run.py invocation). It's a
    real bug under stale_lines_local.py's persistent loop, where "process
    lifetime" would otherwise mean "the entire multi-day run" - the
    40-man roster status cache in particular is the ENTIRE basis of the
    roster-status early-signal check (stale_lines.check_roster_status);
    never invalidating it would mean a player's D10/D15/D60 transition is
    only ever seen on the first poll after the process starts, then
    silently never re-checked again. Called once per loop iteration by
    stale_lines_local.py, before run_poll() - not called by run_poll()
    itself, since the one-shot GitHub Actions path doesn't need it and
    the caches exist specifically to avoid redundant fetches WITHIN a
    single poll (e.g. multiple Betr-lined players on the same team)."""
    _roster_cache.clear()
    _forty_man_cache.clear()


def get_recent_transactions(start_date, end_date):
    """Return the raw list of transactions from /api/v1/transactions
    between start_date and end_date (both "YYYY-MM-DD"). Each entry has
    person.id/fullName, fromTeam/toTeam (either may be None depending on
    transaction type - e.g. an IL placement only has toTeam, an option to
    the minors has both), typeCode (e.g. "OPT" optioned, "SC" status
    change - covers IL placements AND activations, "DES" designated for
    assignment, "REL" released, "OUT" sent outright - see
    stale_lines.classify_transaction for which of these are actually a
    pre-lineup OUT signal vs. noise), and a free-text description. No
    caching here (unlike the roster functions above) - stale_lines.py
    calls this once per poll with a short rolling window, not once per
    player/team, so there's nothing to cache against."""
    data = _get("/transactions", {"sportId": 1, "startDate": start_date, "endDate": end_date})
    return data.get("transactions", [])


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
