"""
Fetches today's posted MLB hitter Fantasy Points lines from Underdog (UD).
The real-money line report.py anchors our projections to instead of relying
solely on a percentile-based curve.

PrizePicks (PP) fetching was removed 2026-08-29: it returned zero lines for
21 straight days (DataDome bot-protection blocking this fetch's IP/UA - see
slips.py's PP-specific calibration note, which already flagged this back on
2026-07-08), and PP's own dashboard products (Premium/Slips/Stacks) were
separately retired 2026-08-18. Downstream code still expects a "pp" key -
compute_pp_ud_ratio/match_lines already derive pp_line from ud_line via the
fallback ratio whenever no PP lines are present, exactly as they've been
doing for those 21 days - so market_lines["pp"] stays a permanent empty
dict rather than removing the schema key.
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

UD_URL = "https://api.underdogfantasy.com/v1/over_under_lines"

HEADERS = {"User-Agent": "Mozilla/5.0"}

# A day's line count is judged against ITS OWN trailing 7-day median, not a
# fixed floor - a flat MIN_EXPECTED_LINES=20 only ever caught total collapse
# (UD hitting 0), not degradation: UD returning 33 on a day that should have
# ~190 sailed past a fixed 20-line floor with zero warning on 2026-08-27
# through -29. COLD_START_MIN_LINES is the fallback only for when there
# isn't yet a week of cached history to compute a median from.
MIN_LINE_RATIO = 0.5
TRAILING_WINDOW_DAYS = 7
COLD_START_MIN_LINES = 20


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


def _today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _trailing_median_ud_count(date_str, days=TRAILING_WINDOW_DAYS):
    """Median UD line count over the most recent `days` cached dates
    strictly before date_str. Returns None if there's no history yet
    (e.g. very early in the season/tracking)."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None
    counts = []
    # Walk backward day by day (not just the last calendar week) so an
    # off-day with no cached file doesn't shrink the sample - cap the
    # lookback so a long gap can't spin forever. A cached 0 is excluded
    # rather than counted as a data point: it means that day's fetch
    # itself failed/was blocked, not that 0 is a legitimate healthy
    # reading - counting it would let an extended outage embedded in the
    # window drag the median (and therefore the floor) down to nothing,
    # defeating the whole check.
    for _ in range(days * 4):
        d -= timedelta(days=1)
        path = os.path.join(CACHE_DIR, f"{d.strftime('%Y-%m-%d')}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            n = len(cached.get("ud", {}))
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


def _healthy_ud_floor(date_str):
    median = _trailing_median_ud_count(date_str)
    if median is None:
        return COLD_START_MIN_LINES
    return max(1, round(MIN_LINE_RATIO * median))


def get_market_lines(date_str, use_cache=True):
    """Return {"ud": {name: line}, "pp": {}}, cached per date. "pp" is
    always empty - see module docstring.

    On a cache hit for TODAY's date specifically, a cached UD count under
    the healthy floor (MIN_LINE_RATIO of the trailing 7-day median)
    triggers a live refetch instead of trusting a stale/thin snapshot -
    UD only adds lines through the day, so a later fetch is always at
    least as good. This is scoped to today only: UD's feed has no
    historical endpoint (it only ever returns the current/upcoming
    board), so refetching for a past date wouldn't refresh anything - it
    would silently splice in the WRONG day's lines. See
    _healthy_ud_floor for the floor calculation.
    """
    cache_path = os.path.join(CACHE_DIR, f"{date_str}.json")
    cached = None
    if use_cache and os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        if date_str != _today_str():
            return cached
        floor = _healthy_ud_floor(date_str)
        if len(cached.get("ud", {})) >= floor:
            return cached
        logger.warning(
            f"Cached UD lines for {date_str} ({len(cached.get('ud', {}))}) are below "
            f"the healthy floor ({floor}, {int(MIN_LINE_RATIO * 100)}% of the trailing "
            f"{TRAILING_WINDOW_DAYS}-day median) - refetching instead of trusting the cache."
        )
        # fall through to refetch

    result = {"ud": {}, "pp": {}}
    try:
        result["ud"] = fetch_underdog_mlb_lines()
    except Exception as e:
        logger.warning(f"Underdog line fetch failed: {e}")
        if cached is not None:
            logger.warning(f"Refetch failed - keeping the existing cached UD lines for {date_str}.")
            return cached

    floor = _healthy_ud_floor(date_str)
    if len(result["ud"]) < floor:
        logger.warning(
            f"UD returned only {len(result['ud'])} lines for {date_str} (expected "
            f"{floor}+, {int(MIN_LINE_RATIO * 100)}% of the trailing {TRAILING_WINDOW_DAYS}-day "
            f"median) - market anchoring will be degraded today."
        )

    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    return result
