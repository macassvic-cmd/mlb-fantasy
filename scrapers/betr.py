"""
Fetches today's posted MLB player prop lines from Betr Picks
(api.fantasy.betr.app - a public, unauthenticated GraphQL API discovered
via the picks.betr.app app bundle; no API key needed for reads).

Unlike Underdog/PrizePicks (market_lines.py), which post a single
"Fantasy Points" line per player, Betr posts several distinct single-stat
and combo markets per player (see STAT_KEYS below) - so the cache here is
keyed by stat type first, then by normalized player name, rather than a
flat {name: value} map.
"""

import json
import os
import re
import unicodedata
import urllib.request

CACHE_DIR = os.path.join("data", "market_lines")

GRAPHQL_URL = "https://api.fantasy.betr.app/graphql"
HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

# Stat keys observed on Betr's board as of 2026-07-20 (15 MLB events, 287
# player-market entries). "H+R+RBI" is the only multi-stat combo offered -
# there is no separate Runs+RBI combo market (RUNS and RUNS_BATTED_IN are
# posted as independent single-stat lines, both at 0.5 only).
HITTER_STAT_KEYS = {
    "HITS", "HITS_RUNS_RUNS_BATTED_IN", "SINGLES", "TOTAL_BASES",
    "RUNS", "RUNS_BATTED_IN", "HITTER_STRIKEOUTS", "WALKS", "FANTASY_POINTS",
}
PITCHER_STAT_KEYS = {
    "EARNED_RUNS", "HITS_ALLOWED", "PITCHING_WALKS", "STRIKEOUTS", "TOTAL_OUTS",
}
STAT_KEYS = HITTER_STAT_KEYS | PITCHER_STAT_KEYS

UPCOMING_EVENTS_QUERY = """query UpcomingEventsInfo($league: League!) {
  getUpcomingEventsV2(league: $league) {
    ... on EventV2 { id date status }
  }
}"""

EVENT_PLAYERS_QUERY = """query EventInfoWithPlayers($id: String!) {
  getEventByIdV2(id: $id) {
    id
    date
    ... on TeamVersusEvent {
      teams {
        id
        name
        players {
          id
          firstName
          lastName
          position
          projections {
            marketId
            marketStatus
            type
            label
            key
            value
            currentValue
          }
        }
      }
    }
  }
}"""


def normalize_name(name):
    name = unicodedata.normalize("NFKD", name or "")
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _graphql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GRAPHQL_URL, data=body, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise RuntimeError(f"Betr GraphQL error: {payload['errors']}")
    return payload["data"]


def fetch_upcoming_mlb_event_ids():
    data = _graphql(UPCOMING_EVENTS_QUERY, {"league": "MLB"})
    return [e["id"] for e in data["getUpcomingEventsV2"]]


def fetch_betr_mlb_lines():
    """Return {stat_key: {normalized_name: value}} for every OPENED market
    across today's MLB slate on Betr."""
    lines = {key: {} for key in STAT_KEYS}

    for event_id in fetch_upcoming_mlb_event_ids():
        data = _graphql(EVENT_PLAYERS_QUERY, {"id": event_id})
        event = data.get("getEventByIdV2")
        if not event or "teams" not in event:
            continue
        for team in event["teams"]:
            for player in team.get("players", []):
                name = f"{player.get('firstName', '')} {player.get('lastName', '')}"
                key_name = normalize_name(name)
                for proj in player.get("projections", []):
                    stat_key = proj.get("key")
                    if stat_key not in STAT_KEYS:
                        continue
                    if proj.get("marketStatus") != "OPENED":
                        continue
                    value = proj.get("value")
                    if value is None:
                        continue
                    lines[stat_key][key_name] = value

    return lines


def load_cached_betr_lines(date_str):
    """Read Betr lines from the per-date cache only (no live fetch)."""
    cache_path = os.path.join(CACHE_DIR, f"betr_{date_str}.json")
    if not os.path.exists(cache_path):
        return None
    with open(cache_path, encoding="utf-8") as f:
        return json.load(f)


def get_betr_lines(date_str, use_cache=True):
    """Return {stat_key: {name: value}}, cached per date."""
    cache_path = os.path.join(CACHE_DIR, f"betr_{date_str}.json")
    if use_cache and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    try:
        result = fetch_betr_mlb_lines()
    except Exception:
        result = {key: {} for key in STAT_KEYS}

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return result
