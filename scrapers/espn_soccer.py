"""
ESPN's public soccer site API (site.api.espn.com) - no auth, no bot
protection (confirmed 2026-09-02, unlike SofaScore). Two uses here:

1. Team names, for fuzzy-matching a Betr club to its ESPN team id (Betr
   and ESPN use different name conventions - "Ballspielverein Borussia 09
   Dortmund" vs "Borussia Dortmund" - matched the same tolerant way
   transfermarkt.py resolves clubs, via normalize_name() substring
   overlap rather than an exact string match).
2. Match rosters, for soccer_dns.py's grading pass: rosters[].roster
   lists every player named to a completed match's 20-man squad, each
   with "starter"/"active" flags - a player absent from BOTH teams'
   roster lists entirely did not appear in the matchday squad at all,
   which is the soccer equivalent of MLB's "not in the starting lineup"
   DNS bet (see stale_lines.py's module docstring for that framing).

NOTE: ESPN's soccer INJURIES endpoint (site.../{league}/injuries) was
checked during the 2026-09-02 feasibility pass and came back
structurally empty for every club tried - not usable as an injury
source, which is why transfermarkt.py exists. This module is only for
team/event resolution and post-match rosters, never injury status.
"""

import json
import logging
import re
import unicodedata
import urllib.request

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0"}
BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

# Betr League enum value -> ESPN league slug, for the 6 domestic leagues
# soccer_dns.py covers. Confirmed live 2026-09-02.
LEAGUE_SLUGS = {
    "EPL": "eng.1",
    "LLG": "esp.1",
    "L1F": "fra.1",
    "BUN": "ger.1",
    "SEA": "ita.1",
    "MLS": "usa.1",
}

_team_cache = {}  # league_slug -> {espn_team_id: normalized_display_name}


def normalize_name(name):
    name = unicodedata.normalize("NFKD", name or "")
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv|fc|cf|afc|sc)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _teams_for_league(league_slug):
    if league_slug not in _team_cache:
        data = _get(f"{BASE}/{league_slug}/teams")
        teams = data["sports"][0]["leagues"][0]["teams"]
        _team_cache[league_slug] = {
            t["team"]["id"]: normalize_name(t["team"]["displayName"]) for t in teams
        }
    return _team_cache[league_slug]


def match_espn_team_id(league_code, team_full_name):
    """Best-effort ESPN team id for a Betr club full_name, via normalized
    substring overlap (same tolerance as transfermarkt.py's alias
    fallback needs - full legal names rarely match ESPN's short display
    names exactly). None if nothing overlaps."""
    league_slug = LEAGUE_SLUGS.get(league_code)
    if not league_slug:
        return None
    try:
        teams = _teams_for_league(league_slug)
    except Exception as e:
        logger.warning(f"ESPN teams fetch failed for {league_slug}: {e}")
        return None
    norm_full = normalize_name(team_full_name)
    for tid, tnorm in teams.items():
        if tnorm and (tnorm in norm_full or norm_full in tnorm):
            return tid
        # word-overlap fallback: share at least one distinctive (len>=4) word
        if set(w for w in tnorm.split() if len(w) >= 4) & set(w for w in norm_full.split() if len(w) >= 4):
            return tid
    return None


def get_scoreboard(league_code, date_str):
    """ESPN scoreboard events for one league on one date (YYYY-MM-DD ->
    ESPN's own YYYYMMDD). Each event has id, date, and competitors."""
    league_slug = LEAGUE_SLUGS.get(league_code)
    if not league_slug:
        return []
    espn_date = date_str.replace("-", "")
    try:
        data = _get(f"{BASE}/{league_slug}/scoreboard?dates={espn_date}")
    except Exception as e:
        logger.warning(f"ESPN scoreboard fetch failed for {league_slug}/{espn_date}: {e}")
        return []
    return data.get("events", [])


def find_espn_event(league_code, home_team_id, away_team_id, date_str):
    """The ESPN event matching both team ids on date_str (checked against
    date_str and the day after, since a UTC-late kickoff can roll onto
    the next ESPN calendar date same as MLB's own yesterday/today/
    tomorrow schedule quirk - see stale_lines.py). None if not found
    (e.g. postponed, or team ids didn't resolve)."""
    from datetime import datetime, timedelta
    if not home_team_id or not away_team_id:
        return None
    base_date = datetime.strptime(date_str, "%Y-%m-%d")
    for offset in (0, 1, -1):
        d = (base_date + timedelta(days=offset)).strftime("%Y-%m-%d")
        for ev in get_scoreboard(league_code, d):
            comp = ev.get("competitions", [{}])[0]
            ids = {c["team"]["id"] for c in comp.get("competitors", [])}
            if {home_team_id, away_team_id} <= ids:
                return ev
    return None


def get_match_squad_names(league_code, event_id):
    """{normalized_name: {"team_id":, "starter":bool, "active":bool}} for
    EVERY player named to either team's matchday squad (rosters[].roster)
    - includes unused substitutes, not just starters. A player whose
    normalized name is absent from this dict entirely did not appear in
    the squad at all for this match - confirmed live 2026-09-02 against a
    completed EPL fixture (20-man rosters per side, each entry carrying
    "starter"/"active" flags). Returns {} if the match summary isn't
    available yet (not played, postponed, or ESPN hasn't posted it) -
    confirmed this is also what an upcoming/unplayed fixture returns, so
    an empty result must NOT be treated as "nobody played"."""
    league_slug = LEAGUE_SLUGS.get(league_code)
    if not league_slug:
        return {}
    try:
        data = _get(f"{BASE}/{league_slug}/summary?event={event_id}")
    except Exception as e:
        logger.warning(f"ESPN summary fetch failed for {league_slug}/{event_id}: {e}")
        return {}

    squad = {}
    for team_roster in data.get("rosters", []):
        team_id = team_roster.get("team", {}).get("id")
        for p in team_roster.get("roster", []):
            athlete = p.get("athlete", {})
            name = athlete.get("fullName") or athlete.get("displayName")
            if not name:
                continue
            squad[normalize_name(name)] = {
                "team_id": team_id,
                "starter": bool(p.get("starter")),
                "active": bool(p.get("active")),
            }
    return squad
