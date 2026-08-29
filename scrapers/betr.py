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
import logging
import os
import re
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join("data", "market_lines")

GRAPHQL_URL = "https://api.fantasy.betr.app/graphql"
HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

# H+R+RBI is Betr's most reliable market (100-157 players/day observed) -
# judged against its OWN trailing 7-day median rather than a fixed floor,
# same reasoning as market_lines.py's UD check: a flat MIN_EXPECTED=40 only
# ever caught total collapse, not degradation (34 vs. a ~180 normal sailed
# through unflagged 2026-08-27 through -29). COLD_START_MIN_HRRBI_LINES is
# the fallback only for when there isn't yet a week of cached history.
MIN_HRRBI_RATIO = 0.5
TRAILING_WINDOW_DAYS = 7
COLD_START_MIN_HRRBI_LINES = 40

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


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _trailing_median_hrrbi_count(date_str, days=TRAILING_WINDOW_DAYS):
    """Median Betr H+R+RBI line count over the most recent `days` cached
    dates strictly before date_str. Returns None if there's no history yet."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    counts = []
    # A cached 0 is excluded rather than counted as a data point - it means
    # that day's fetch failed/was blocked, not that 0 is a legitimate
    # healthy reading. Counting it would let an extended outage embedded in
    # the window (e.g. 2026-08-21 through -25's 5-day total outage) drag
    # the median - and therefore the floor - down to nothing.
    for _ in range(days * 4):
        d -= timedelta(days=1)
        path = os.path.join(CACHE_DIR, f"betr_{d.strftime('%Y-%m-%d')}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            n = len(cached.get("HITS_RUNS_RUNS_BATTED_IN", {}))
        except Exception:
            continue
        if n == 0:
            continue
        counts.append(n)
        if len(counts) >= days:
            break
    if not counts:
        return None
    counts.sort()
    n = len(counts)
    mid = n // 2
    return counts[mid] if n % 2 else (counts[mid - 1] + counts[mid]) / 2


def _healthy_hrrbi_floor(date_str):
    median = _trailing_median_hrrbi_count(date_str)
    if median is None:
        return COLD_START_MIN_HRRBI_LINES
    return max(1, round(MIN_HRRBI_RATIO * median))


def get_betr_lines(date_str, use_cache=True):
    """Return {stat_key: {name: value}}, cached per date.

    On a cache hit for TODAY's date specifically, an H+R+RBI count under
    the healthy floor (MIN_HRRBI_RATIO of the trailing 7-day median)
    triggers a live refetch instead of trusting a thin snapshot - lines
    only get added through the day. Scoped to today only, same reasoning
    as market_lines.get_market_lines: Betr's feed has no historical
    endpoint, so refetching for a past date would splice in the wrong
    day's lines rather than fix anything.
    """
    cache_path = os.path.join(CACHE_DIR, f"betr_{date_str}.json")
    cached = None
    if use_cache and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        if date_str != _today_str():
            return cached
        floor = _healthy_hrrbi_floor(date_str)
        if len(cached.get("HITS_RUNS_RUNS_BATTED_IN", {})) >= floor:
            return cached
        logger.warning(
            f"Cached Betr H+R+RBI lines for {date_str} "
            f"({len(cached.get('HITS_RUNS_RUNS_BATTED_IN', {}))}) are below the healthy "
            f"floor ({floor}, {int(MIN_HRRBI_RATIO * 100)}% of the trailing "
            f"{TRAILING_WINDOW_DAYS}-day median) - refetching instead of trusting the cache."
        )
        # fall through to refetch

    try:
        result = fetch_betr_mlb_lines()
    except Exception as e:
        logger.warning(f"Betr line fetch failed: {e}")
        if cached is not None:
            logger.warning(f"Refetch failed - keeping the existing cached Betr lines for {date_str}.")
            return cached
        result = {key: {} for key in STAT_KEYS}

    n_hrrbi = len(result.get("HITS_RUNS_RUNS_BATTED_IN", {}))
    floor = _healthy_hrrbi_floor(date_str)
    if n_hrrbi < floor:
        logger.warning(
            f"Betr H+R+RBI returned only {n_hrrbi} lines for {date_str} (expected "
            f"{floor}+, {int(MIN_HRRBI_RATIO * 100)}% of the trailing {TRAILING_WINDOW_DAYS}-day "
            f"median) - fetch may be degraded."
        )

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return result
