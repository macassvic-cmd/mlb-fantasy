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


# ---------------------------------------------------------------------------
# Platoon splits (real vs-LHP/vs-RHP and vs-LHB/vs-RHB, not an estimate)
#
# FanGraphs' own splits leaderboard isn't exposed via pybaseball, but
# Baseball-Reference's player split pages are, via pybaseball.get_splits().
# These are real box-score splits, not a guess. ERA/FIP are NOT available
# split by handedness from Baseball-Reference or any other free source we
# have access to - only the underlying counting stats (PA/AB/BB/SO/hits),
# from which we derive wOBA, K%, and BB% ourselves.
# ---------------------------------------------------------------------------

MIN_SPLIT_PA = 20  # below this a split is too noisy to trust as a signal

# Static (FanGraphs-style) linear weights for wOBA - not season-specific,
# but close enough for a platoon-strength signal.
_WOBA_W = {"bb": 0.69, "hbp": 0.719, "1b": 0.87, "2b": 1.217, "3b": 1.529, "hr": 1.94}


def _woba_from_split_row(row):
    bb = (row.get("BB") or 0) - (row.get("IBB") or 0)
    hbp = row.get("HBP") or 0
    singles = row.get("1B") or 0
    doubles = row.get("2B") or 0
    triples = row.get("3B") or 0
    hr = row.get("HR") or 0
    ab = row.get("AB") or 0
    sf = row.get("SF") or 0

    denom = ab + bb + hbp + sf
    if denom <= 0:
        return None
    numer = (_WOBA_W["bb"] * bb + _WOBA_W["hbp"] * hbp + _WOBA_W["1b"] * singles
             + _WOBA_W["2b"] * doubles + _WOBA_W["3b"] * triples + _WOBA_W["hr"] * hr)
    return round(numer / denom, 3)


def _split_summary(row):
    if row is None:
        return None
    pa = int(row.get("PA") or 0)
    so = row.get("SO") or 0
    bb = row.get("BB") or 0
    return {
        "pa": pa,
        "woba": _woba_from_split_row(row),
        "ops": _safe_float(row.get("OPS")),
        "k_pct": round(so / pa, 3) if pa else None,
        "bb_pct": round(bb / pa, 3) if pa else None,
    }


def _bbref_id_map():
    """MLB-id -> Baseball-Reference-id for every player, loaded once."""
    if "bbref_map" not in _cache:
        from pybaseball import chadwick_register
        df = call_with_timeout(chadwick_register, timeout_s=90, label="chadwick_register")
        if df is None or df.empty:
            logger.warning("chadwick_register fetch failed or timed out - no platoon splits available")
            _cache["bbref_map"] = {}
        else:
            _cache["bbref_map"] = dict(zip(df["key_mlbam"], df["key_bbref"]))
    return _cache["bbref_map"]


def _bbref_id_for(mlbam_id):
    return _bbref_id_map().get(mlbam_id)


def get_batter_platoon_splits(mlbam_id, season=None):
    """Real vs-RHP / vs-LHP splits (wOBA, OPS, K%, BB%, PA) for this season
    from Baseball-Reference. Returns {} if unmatched or no data yet."""
    season = season or datetime.now().year
    key = f"bat_split_{mlbam_id}_{season}"
    if key in _cache:
        return _cache[key]

    bbref_id = _bbref_id_for(mlbam_id)
    if not bbref_id:
        _cache[key] = {}
        return {}

    from pybaseball import get_splits
    df = call_with_timeout(
        get_splits, bbref_id, season,
        timeout_s=60, label=f"get_splits(batter {mlbam_id})",
    )
    result = {}
    if df is not None and not df.empty:
        try:
            if ("Platoon Splits", "vs RHP") in df.index:
                result["vs_rhp"] = _split_summary(df.loc[("Platoon Splits", "vs RHP")])
            if ("Platoon Splits", "vs LHP") in df.index:
                result["vs_lhp"] = _split_summary(df.loc[("Platoon Splits", "vs LHP")])
        except Exception as e:
            logger.warning(f"platoon split parse failed for batter {mlbam_id}: {e}")
            result = {}

    _cache[key] = result
    return result


def get_pitcher_platoon_splits(mlbam_id, season=None):
    """Real vs-RHB / vs-LHB splits *against* this pitcher (wOBA-against,
    OPS-against, K%, BB%, PA) for this season from Baseball-Reference."""
    season = season or datetime.now().year
    key = f"pit_split_{mlbam_id}_{season}"
    if key in _cache:
        return _cache[key]

    bbref_id = _bbref_id_for(mlbam_id)
    if not bbref_id:
        _cache[key] = {}
        return {}

    from pybaseball import get_splits
    res = call_with_timeout(
        get_splits, bbref_id, season, pitching_splits=True,
        timeout_s=60, label=f"get_splits(pitcher {mlbam_id})",
    )
    df = res[0] if isinstance(res, tuple) else res  # batting-against table
    result = {}
    if df is not None and not df.empty:
        try:
            if ("Platoon Splits", "vs RHB") in df.index:
                result["vs_rhb"] = _split_summary(df.loc[("Platoon Splits", "vs RHB")])
            if ("Platoon Splits", "vs LHB") in df.index:
                result["vs_lhb"] = _split_summary(df.loc[("Platoon Splits", "vs LHB")])
        except Exception as e:
            logger.warning(f"platoon split parse failed for pitcher {mlbam_id}: {e}")
            result = {}

    _cache[key] = result
    return result


def match_platoon_matchup(batter_mlbam_id, bat_side, pitcher_mlbam_id, pitcher_hand, season=None):
    """Match a batter against today's actual opposing starter's handedness-
    specific splits. Falls back to None (no signal) when either side's
    split has too few PA (< MIN_SPLIT_PA) to trust, or is unavailable."""
    if not batter_mlbam_id or not pitcher_mlbam_id:
        return {"platoon_same_hand": bat_side == pitcher_hand, "advantage": None}

    batter_splits = get_batter_platoon_splits(batter_mlbam_id, season)
    pitcher_splits = get_pitcher_platoon_splits(pitcher_mlbam_id, season)

    bat_key = "vs_lhp" if pitcher_hand == "L" else "vs_rhp"
    pit_key = "vs_lhb" if bat_side == "L" else "vs_rhb"
    bat_split = batter_splits.get(bat_key)
    pit_split = pitcher_splits.get(pit_key)

    bat_woba = bat_split["woba"] if bat_split and bat_split["pa"] >= MIN_SPLIT_PA else None
    pit_woba = pit_split["woba"] if pit_split and pit_split["pa"] >= MIN_SPLIT_PA else None

    base = {"platoon_same_hand": bat_side == pitcher_hand}
    if bat_woba is None or pit_woba is None:
        base["advantage"] = None
        return base

    edge = round(bat_woba - pit_woba, 3)
    base.update({
        "batter_woba": bat_woba,
        "batter_split_pa": bat_split["pa"],
        "batter_split_label": "vs LHP" if pitcher_hand == "L" else "vs RHP",
        "pitcher_woba_against": pit_woba,
        "pitcher_split_pa": pit_split["pa"],
        "pitcher_split_label": "vs LHB" if bat_side == "L" else "vs RHB",
        "edge_woba": edge,
        "advantage": "batter" if edge > 0.010 else ("pitcher" if edge < -0.010 else "neutral"),
    })
    return base
