"""
In-repo port of the Dabble soccer board producer (2026-09-14 production-
scheduling work) - previously this repo's ENTIRE Soccer DNS pipeline
depended on an external script at C:\\platform-tools\\dabble_board_
producer.py that only exists on one local Windows machine. That made
"do not rely on my PC being awake" structurally impossible: a GitHub
Actions runner has no access to that path at all. This module is the
same logic (verified live against the real API, same schema, same
canonicalization) checked into the repo so both local dev and CI can
call it identically via a plain Python import - no subprocess, no
external file dependency.

Hits Dabble's real public API directly (api.dabble.com) - confirmed live
2026-09-14 this needs no authentication for the DFS props endpoints used
here. See soccer_adapter.py's module docstring for the broader
architecture this feeds (Dabble board IS the candidate universe).
"""

import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

BASE = "https://api.dabble.com"
TIMEOUT = 20
REQUEST_PAUSE = 0.05
SOCCER_COMPETITION_CODE = "SOC"

SPORT_NAME_MAP = {
    "baseball": "mlb",
    "football": "soccer",  # Dabble's name for association football
    "basketball": "nba",
    "ice hockey": "nhl",
    "american football": "nfl",
}

_session = requests.Session()
_session.headers.update({"Accept": "application/json", "User-Agent": "dabble-soccer-producer/1.0"})


def _get_json(url, params=None):
    r = _session.get(url, params=params, timeout=TIMEOUT)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code} for {r.url}\n{r.text[:1200]}")
    return r.json()


def get_all_competitions():
    """The broad /competitions list (not /active) - confirmed live
    2026-09-14 that /active misses leagues with real live markets
    (Ligue 1 had 100+ live props while absent from /active entirely)."""
    body = _get_json(f"{BASE}/competitions")
    return body.get("data", []) if isinstance(body, dict) else []


def get_soccer_competitions():
    return [c for c in get_all_competitions() if c.get("code") == SOCCER_COMPETITION_CODE]


def get_market_groups(competition_id):
    url = f"{BASE}/search/dfs/competitions/{quote(str(competition_id), safe='')}/market-groups"
    body = _get_json(url)
    rows = body.get("data", []) if isinstance(body, dict) else []
    groups = []
    for row in rows:
        if isinstance(row, dict):
            name = row.get("marketGroupName")
            if name and name not in groups:
                groups.append(name)
    return groups


def get_group_props(competition_id, market_group):
    url = f"{BASE}/search/dfs/competitions/{quote(str(competition_id), safe='')}/props"
    out = []
    cursor = None
    while True:
        params = {"marketGroupName": market_group}
        if cursor:
            params["next"] = cursor
        body = _get_json(url, params=params)
        data = body.get("data", []) if isinstance(body, dict) else []
        cursor = body.get("next") if isinstance(body, dict) else None
        if isinstance(data, list):
            out.extend(data)
        if not cursor:
            break
        time.sleep(REQUEST_PAUSE)
    return out


def canonicalize(prop):
    competition = prop.get("competition") or {}
    sport_obj = prop.get("sport") or {}
    league = competition.get("displayName") or competition.get("name") if isinstance(competition, dict) else None
    sport_name_raw = str(sport_obj.get("name") or "").strip().lower()
    sport = SPORT_NAME_MAP.get(sport_name_raw, sport_name_raw or "unknown")

    record = {
        "player_name": prop.get("playerName"),
        "player_id": prop.get("playerId"),
        "team": prop.get("teamAbbreviation") or prop.get("teamName"),
        "market": prop.get("marketGroupName"),
        "line": prop.get("propValue"),
        "event_date": prop.get("fixtureDate"),
        "sport": sport,
        "league": league or sport.upper(),
        "matchup": prop.get("fixtureDisplayName") or prop.get("fixtureName"),
        "position": prop.get("playerPosition") or "",
        "fixture_id": prop.get("fixtureId"),
        # Confirmed live 2026-09-14: Dabble's DFS prop payload has NO
        # injury/availability field at all - see soccer_adapter.py.
        "dabble_status_raw": None,
        "dabble_status_normalized": None,
    }
    required = ("player_name", "team", "market", "line", "event_date")
    if any(record.get(k) in (None, "") for k in required):
        return None
    return record


def fetch_competition_props(comp):
    cid = comp["id"]
    groups = get_market_groups(cid)
    raw_props = []
    for group in groups:
        rows = get_group_props(cid, group)
        raw_props.extend(rows)
        time.sleep(REQUEST_PAUSE)

    deduped = {}
    for p in raw_props:
        if not isinstance(p, dict):
            continue
        key = p.get("id") or (p.get("fixtureId"), p.get("playerId"), p.get("marketId"),
                               p.get("marketGroupName"), p.get("propValue"))
        deduped[key] = p
    return list(deduped.values())


def build_soccer_board():
    """{sport, source, generated_at, competitions, props} across EVERY
    soccer competition Dabble currently lists - the candidate universe
    must include every league Dabble covers, not a hand-picked subset."""
    comps = get_soccer_competitions()
    all_raw = []
    competitions_seen = []
    for comp in comps:
        try:
            rows = fetch_competition_props(comp)
        except Exception as e:
            print(f"dabble_soccer_producer: {comp.get('displayName') or comp.get('name')} failed (non-fatal): {e}")
            continue
        if rows:
            competitions_seen.append(comp.get("displayName") or comp.get("name"))
        all_raw.extend(rows)
        time.sleep(REQUEST_PAUSE)

    canonical = [r for r in (canonicalize(p) for p in all_raw) if r is not None]
    canonical.sort(key=lambda x: (x.get("event_date") or "", x.get("team") or "",
                                   x.get("player_name") or "", x.get("market") or "", str(x.get("line") or "")))
    return {
        "sport": "soccer", "source": "dabble",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "competitions": competitions_seen, "props": canonical,
    }


def produce_soccer_board(output_path):
    """Fetches the live board and atomically writes it to output_path -
    the one function soccer_daily_digest.py's fresh-fetch step calls."""
    import json
    import os
    from pathlib import Path

    payload = build_soccer_board()
    path = Path(output_path)
    os.makedirs(path.parent, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return payload
