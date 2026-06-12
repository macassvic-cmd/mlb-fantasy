"""
Fetches today's posted MLB hitter Fantasy Points lines from Underdog (UD)
and PrizePicks (PP). These are the real-money lines we should be projecting
near - used in report.py to anchor our projections instead of relying solely
on a percentile-based curve.
"""

import json
import os
import re
import unicodedata
import urllib.request

CACHE_DIR = os.path.join("data", "market_lines")

UD_URL = "https://api.underdogfantasy.com/v1/over_under_lines"
PP_URL = "https://api.prizepicks.com/projections?league_id=2"

HEADERS = {"User-Agent": "Mozilla/5.0"}


def normalize_name(name):
    name = unicodedata.normalize("NFKD", name or "")
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def fetch_underdog_mlb_lines():
    """Return {normalized_name: fantasy_points_line} for MLB hitters/pitchers on Underdog."""
    data = _fetch_json(UD_URL)
    mlb_games = {g["id"] for g in data["games"] if g.get("sport_id") == "MLB"}
    appearances = {a["id"]: a for a in data["appearances"] if a["match_id"] in mlb_games}
    players = {p["id"]: p for p in data["players"]}

    lines = {}
    for line in data["over_under_lines"]:
        ou = line.get("over_under") or {}
        appstat = ou.get("appearance_stat") or {}
        if appstat.get("display_stat") != "Fantasy Points":
            continue
        appearance = appearances.get(appstat.get("appearance_id"))
        if not appearance:
            continue
        player = players.get(appearance["player_id"])
        if not player:
            continue
        try:
            value = float(line.get("stat_value"))
        except (TypeError, ValueError):
            continue
        name = f"{player.get('first_name', '')} {player.get('last_name', '')}"
        lines[normalize_name(name)] = value

    return lines


def fetch_prizepicks_mlb_lines():
    """Return {normalized_name: fantasy_points_line} for MLB hitters on PrizePicks."""
    data = _fetch_json(PP_URL)
    players = {x["id"]: x for x in data["included"] if x["type"] == "new_player"}

    lines = {}
    for d in data["data"]:
        attr = d["attributes"]
        if attr.get("stat_type") not in ("Hitter Fantasy Score", "Pitcher Fantasy Score"):
            continue
        if attr.get("odds_type") != "standard":
            continue
        rel = (d.get("relationships", {}).get("new_player", {}) or {}).get("data")
        if not rel:
            continue
        player = players.get(rel["id"])
        if not player:
            continue
        try:
            value = float(attr.get("line_score"))
        except (TypeError, ValueError):
            continue
        name = player.get("attributes", {}).get("name", "")
        lines[normalize_name(name)] = value

    return lines


def compute_pp_ud_ratio(market_lines):
    """Average PP/UD line ratio across players with both lines posted."""
    ud_lines = (market_lines or {}).get("ud", {})
    pp_lines = (market_lines or {}).get("pp", {})
    pairs = [pp_lines[k] / ud_lines[k] for k in ud_lines if k in pp_lines and ud_lines[k] > 0]
    return sum(pairs) / len(pairs) if pairs else 0.86


def match_lines(name, market_lines, pp_ud_ratio=None):
    """Return (ud_line, pp_line) for a player, deriving whichever side is
    missing from the other via pp_ud_ratio. Returns (None, None) if neither
    book has posted a line for this player."""
    ud_lines = (market_lines or {}).get("ud", {})
    pp_lines = (market_lines or {}).get("pp", {})
    key = normalize_name(name)
    ud_line = ud_lines.get(key)
    pp_line = pp_lines.get(key)
    if ud_line is None and pp_line is None:
        return None, None
    if pp_ud_ratio is None:
        pp_ud_ratio = compute_pp_ud_ratio(market_lines)
    if ud_line is None:
        ud_line = pp_line / pp_ud_ratio
    elif pp_line is None:
        pp_line = ud_line * pp_ud_ratio
    return ud_line, pp_line


def load_cached_market_lines(date_str):
    """Read market lines from the per-date cache only (no live fetch).
    Returns None if no cache exists for that date - used by the backtest so
    historical dates without a saved snapshot are skipped cleanly."""
    cache_path = os.path.join(CACHE_DIR, f"{date_str}.json")
    if not os.path.exists(cache_path):
        return None
    with open(cache_path, encoding="utf-8") as f:
        return json.load(f)


def get_market_lines(date_str, use_cache=True):
    """Return {"ud": {name: line}, "pp": {name: line}}, cached per date."""
    cache_path = os.path.join(CACHE_DIR, f"{date_str}.json")
    if use_cache and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            return json.load(f)

    result = {"ud": {}, "pp": {}}
    try:
        result["ud"] = fetch_underdog_mlb_lines()
    except Exception:
        pass
    try:
        result["pp"] = fetch_prizepicks_mlb_lines()
    except Exception:
        pass

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return result
