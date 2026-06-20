"""
FanGraphs data via pybaseball.
Pulls wOBA, wRC+, BB%, K%, ISO, BABIP for batters and pitchers.
Data is fetched once per session and cached in memory.
"""

import logging
import warnings
from datetime import datetime

from scrapers._timeout import call_with_timeout

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

_cache = {}


def _batting_df(season):
    key = f"bat_{season}"
    if key not in _cache:
        import pandas as pd
        from pybaseball import batting_stats
        df = call_with_timeout(
            batting_stats, season, qual=20,
            timeout_s=60, label="FanGraphs batting_stats",
        )
        if df is None:
            logger.warning("FanGraphs batting fetch failed or timed out")
            df = pd.DataFrame()
        else:
            logger.info(f"FanGraphs batting: {len(df)} players loaded")
        _cache[key] = df
    return _cache[key]


def _pitching_df(season):
    key = f"pit_{season}"
    if key not in _cache:
        import pandas as pd
        from pybaseball import pitching_stats
        df = call_with_timeout(
            pitching_stats, season, qual=10,
            timeout_s=60, label="FanGraphs pitching_stats",
        )
        if df is None:
            logger.warning("FanGraphs pitching fetch failed or timed out")
            df = pd.DataFrame()
        else:
            logger.info(f"FanGraphs pitching: {len(df)} pitchers loaded")
        _cache[key] = df
    return _cache[key]


def _find_row(df, name):
    """Find a player row by name — exact, then last-name fuzzy."""
    if df.empty or "Name" not in df.columns:
        return None
    exact = df[df["Name"] == name]
    if not exact.empty:
        return exact.iloc[0]
    last = name.split()[-1]
    partial = df[df["Name"].str.contains(last, case=False, na=False, regex=False)]
    if not partial.empty:
        return partial.iloc[0]
    return None


def _safe_float(val):
    if val is None:
        return None
    try:
        if isinstance(val, str):
            val = val.replace("%", "").strip()
        return round(float(val), 3)
    except (ValueError, TypeError):
        return None


def _pct(val):
    """Convert '12.3%' or 0.123 to a decimal proportion."""
    f = _safe_float(val)
    if f is None:
        return None
    return round(f / 100, 3) if f > 1 else f


def get_batter_fg_stats(player_name, season=None):
    season = season or datetime.now().year
    df = _batting_df(season)
    row = _find_row(df, player_name)
    if row is None:
        return {}
    return {
        "woba": _safe_float(row.get("wOBA")),
        "wrc_plus": _safe_float(row.get("wRC+")),
        "bb_pct": _pct(row.get("BB%")),
        "k_pct": _pct(row.get("K%")),
        "iso": _safe_float(row.get("ISO")),
        "babip": _safe_float(row.get("BABIP")),
        "avg_fg": _safe_float(row.get("AVG")),
        "obp_fg": _safe_float(row.get("OBP")),
        "slg_fg": _safe_float(row.get("SLG")),
        "pa": _safe_float(row.get("PA")),
    }


def get_pitcher_fg_stats(pitcher_name, season=None):
    season = season or datetime.now().year
    df = _pitching_df(season)
    row = _find_row(df, pitcher_name)
    if row is None:
        return {}
    return {
        "fip": _safe_float(row.get("FIP")),
        "xfip": _safe_float(row.get("xFIP")),
        "siera": _safe_float(row.get("SIERA")),
        "k_pct": _pct(row.get("K%")),
        "bb_pct": _pct(row.get("BB%")),
        "era_fg": _safe_float(row.get("ERA")),
        "whip_fg": _safe_float(row.get("WHIP")),
    }


def get_platoon_splits(player_name, bat_side, pitcher_hand, season=None):
    """
    Estimate platoon-adjusted wOBA.
    FanGraphs platoon splits aren't available via pybaseball, so we apply
    standard platoon factors to the overall wOBA.
    Typical advantage: same-hand matchup suppresses wOBA by ~15–20 pts.
    """
    season = season or datetime.now().year
    stats = get_batter_fg_stats(player_name, season)
    woba = stats.get("woba") or 0.320

    # Platoon factor: batter has advantage vs opposite-hand pitcher
    same_hand = (bat_side == pitcher_hand)
    if same_hand:
        adj_woba = round(woba * 0.94, 3)   # same-hand: batter disadvantaged
    else:
        adj_woba = round(woba * 1.06, 3)   # opposite-hand: batter advantaged

    return {
        "woba_vs_pitcher": adj_woba,
        "platoon_same_hand": same_hand,
    }
