"""
ESPN's public site/core APIs for NFL/NBA/NHL - opened 2026-09-09 to extend
the JSON-driven DNS detector (see board_loader.py, pro_league_dns.py)
beyond MLB/soccer. Two ESPN surfaces, same pattern espn_soccer.py already
uses for soccer:

1. site.api.espn.com - team lists (for fuzzy-matching a book's team name
   to an ESPN team id, same normalize_name()-based substring/word-overlap
   tolerance as espn_soccer.match_espn_team_id) and, later, boxscore data
   for grading once a real completed game is available to verify the
   response shape against (not built yet - see pro_league_dns.py's
   module docstring for why grading is deferred to a later phase).

2. sports.core.api.espn.com/.../teams/{id}/injuries - confirmed live
   2026-09-09 to return real data for all three leagues, UNLIKE ESPN's
   soccer injuries endpoint (see espn_soccer.py's own docstring - that
   one came back structurally empty, which is why soccer uses
   Transfermarkt instead). IMPORTANT: this endpoint is a time-ordered
   STATUS LOG, not a current-status list - one team's log had 55 entries
   including plenty of "Active" (i.e. no longer hurt) mixed in among
   "Out"/"Injured Reserve" ones, sometimes many entries for the same
   player as their status changed over the season. fetch_team_injuries()
   resolves every entry, keeps only each player's MOST RECENT one by
   date, and only then checks whether that latest status counts as
   "confirmed out" - trusting the raw item count/status directly (like
   naively taking the first page) would misflag a healthy player whose
   only OTHER appearance in the log is an old "Out" from weeks ago.

Each league's confirmed_out_statuses/not_confirmed_statuses were built
from a small live sample (preseason, so sparse) plus known common ESPN
designations - not exhaustively verified across a full season. A status
seen that's in neither set is treated as NOT confirmed (conservative
default - under-flagging is far cheaper than a wrong "did not play" bet)
and logged for visibility so the sets can be refined as real data comes
in, the same lesson board_loader.py's pitcher filter already learned
from a real false positive.
"""

import json
import logging
import os
import urllib.request

from scrapers.espn_soccer import normalize_name

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0"}
SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports"
CORE_BASE = "https://sports.core.api.espn.com/v2/sports"

LEAGUE_CONFIG = {
    "nfl": {
        "sport_slug": "football", "league_slug": "nfl", "emoji": "🏈",
        "confirmed_out_statuses": {
            "out", "injured reserve", "reserve/injured", "physically unable to perform",
            "pup", "non-football injury", "reserve/suspended", "suspended",
            "reserve/pup", "out for season", "reserve/out",
        },
        "not_confirmed_statuses": {"active", "questionable", "doubtful", "probable", "game-time decision"},
    },
    "nba": {
        "sport_slug": "basketball", "league_slug": "nba", "emoji": "🏀",
        "confirmed_out_statuses": {"out", "out for season", "suspended", "injured reserve"},
        "not_confirmed_statuses": {"active", "questionable", "doubtful", "probable", "day-to-day", "day to day", "game-time decision"},
    },
    "nhl": {
        "sport_slug": "hockey", "league_slug": "nhl", "emoji": "🏒",
        "confirmed_out_statuses": {
            "out", "injured reserve", "injured non-roster", "suspension", "suspended",
            "out for season", "long term injured reserve", "ltir",
        },
        "not_confirmed_statuses": {"active", "questionable", "doubtful", "probable", "day-to-day", "day to day", "game-time decision"},
    },
}

_team_cache = {}  # league -> {espn_team_id: normalized_display_name}


def _get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _teams_for_league(league):
    if league not in _team_cache:
        cfg = LEAGUE_CONFIG[league]
        data = _get(f"{SITE_BASE}/{cfg['sport_slug']}/{cfg['league_slug']}/teams")
        teams = data["sports"][0]["leagues"][0]["teams"]
        _team_cache[league] = {t["team"]["id"]: normalize_name(t["team"]["displayName"]) for t in teams}
    return _team_cache[league]


def match_espn_team_id(league, team_name):
    """Same tolerant matching as espn_soccer.match_espn_team_id - a
    book's team name rarely matches ESPN's display name exactly."""
    cfg = LEAGUE_CONFIG.get(league)
    if not cfg:
        return None
    try:
        teams = _teams_for_league(league)
    except Exception as e:
        logger.warning(f"ESPN {league} teams fetch failed: {e}")
        return None
    norm_full = normalize_name(team_name)
    for tid, tnorm in teams.items():
        if tnorm and (tnorm in norm_full or norm_full in tnorm):
            return tid
        if set(w for w in tnorm.split() if len(w) >= 4) & set(w for w in norm_full.split() if len(w) >= 4):
            return tid
    return None


def _fetch_all_injury_refs(cfg, team_id):
    refs = []
    page = 1
    while True:
        url = f"{CORE_BASE}/{cfg['sport_slug']}/leagues/{cfg['league_slug']}/teams/{team_id}/injuries?limit=100&page={page}"
        data = _get(url)
        refs.extend(item["$ref"] for item in data.get("items", []))
        if page >= data.get("pageCount", 1):
            break
        page += 1
    return refs


def fetch_team_injuries(league, team_id):
    """[{name, normalized_name, status, since, expected_return_date}] for
    every player on this team whose MOST RECENT status-log entry is a
    "confirmed out" designation (see module docstring for why "most
    recent" matters - the raw log includes stale/superseded entries).
    expected_return_date is a "YYYY-MM-DD" string or None (matches
    scrapers/transfermarkt.py's shape closely enough that
    pro_league_dns.py's confirmed-out-through-fixture gate is identical
    to soccer_dns.py's)."""
    cfg = LEAGUE_CONFIG[league]
    try:
        refs = _fetch_all_injury_refs(cfg, team_id)
    except Exception as e:
        logger.warning(f"ESPN {league} injuries fetch failed for team {team_id}: {e}")
        return []

    latest_by_athlete = {}
    for ref in refs:
        try:
            d = _get(ref)
        except Exception as e:
            logger.warning(f"ESPN {league} injury detail fetch failed: {e}")
            continue
        athlete_ref = (d.get("athlete") or {}).get("$ref")
        if not athlete_ref:
            continue
        athlete_id = athlete_ref.rstrip("/").split("/")[-1].split("?")[0]
        date = d.get("date") or ""
        existing = latest_by_athlete.get(athlete_id)
        if existing is None or date > existing["date"]:
            latest_by_athlete[athlete_id] = {
                "athlete_ref": athlete_ref, "date": date, "status": d.get("status") or "",
                "return_date": (d.get("details") or {}).get("returnDate"),
            }

    results = []
    for info in latest_by_athlete.values():
        status_norm = info["status"].strip().lower()
        if status_norm not in cfg["confirmed_out_statuses"]:
            if status_norm and status_norm not in cfg["not_confirmed_statuses"]:
                logger.info(f"ESPN {league}: unrecognized injury status {info['status']!r} - not treated as confirmed-out")
            continue
        try:
            athlete = _get(info["athlete_ref"])
        except Exception as e:
            logger.warning(f"ESPN {league} athlete fetch failed: {e}")
            continue
        name = athlete.get("displayName") or athlete.get("fullName")
        if not name:
            continue
        results.append({
            "name": name, "normalized_name": normalize_name(name),
            "status": info["status"], "since": (info["date"][:10] if info["date"] else None),
            "expected_return_date": info["return_date"],
        })
    return results


def get_team_injuries_cached(league, team_id, cache_date_str):
    """fetch_team_injuries() cached to data/espn_pro_leagues/injuries/
    {league}/{cache_date_str}/{team_id}.json - one fetch per team per
    calendar day, same reasoning as transfermarkt.get_team_injuries_cached
    (this backs a once/twice-daily scan, not a live poller, and the
    status-log resolution above can be dozens of requests per team)."""
    day_dir = os.path.join("data", "espn_pro_leagues", "injuries", league, cache_date_str)
    cache_path = os.path.join(day_dir, f"{team_id}.json")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    records = fetch_team_injuries(league, team_id)
    os.makedirs(day_dir, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    return records
