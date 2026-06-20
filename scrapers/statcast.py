"""
Statcast data via pybaseball.
Calculates xwOBA, barrel%, hard-hit%, EV stats, and DOC (damage on middle-middle).
"""

import logging
import warnings
import pandas as pd
from datetime import datetime, timedelta

from scrapers._timeout import call_with_timeout

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

# pybaseball caches to disk automatically — suppress its progress bars
try:
    from pybaseball import statcast_batter, cache
    cache.enable()
except ImportError:
    logger.warning("pybaseball not installed; Statcast data unavailable")
    statcast_batter = None


def _fetch(player_id, start_dt, end_dt):
    if statcast_batter is None:
        return pd.DataFrame()
    df = call_with_timeout(
        statcast_batter, start_dt, end_dt, player_id,
        timeout_s=60, label=f"statcast_batter({player_id})",
    )
    return df if df is not None and not df.empty else pd.DataFrame()


def _ev_stats(df, suffix=""):
    """Compute EV-based metrics from a Statcast DataFrame slice."""
    if df.empty:
        return {}

    ev = df["launch_speed"].dropna() if "launch_speed" in df.columns else pd.Series([], dtype=float)
    if ev.empty:
        return {}

    bbe = len(ev)
    stats = {
        f"avg_ev{suffix}": round(float(ev.mean()), 1),
        f"ev_stdev{suffix}": round(float(ev.std()), 1),
        f"ev_floor{suffix}": round(float(ev.quantile(0.10)), 1),
        f"ev_ceiling{suffix}": round(float(ev.quantile(0.90)), 1),
        f"hard_hit_pct{suffix}": round(float((ev >= 95).sum() / bbe * 100), 1),
    }

    # Barrel% — Statcast encodes batted-ball quality in launch_speed_angle;
    # a value of 6 denotes a "Barrel".
    if "launch_speed_angle" in df.columns:
        barrels = int((df["launch_speed_angle"] == 6).sum())
        stats[f"barrel_pct{suffix}"] = round(barrels / bbe * 100, 1)

    # xwOBA
    if "estimated_woba_using_speedangle" in df.columns:
        xwoba_vals = df["estimated_woba_using_speedangle"].dropna()
        if not xwoba_vals.empty:
            stats[f"xwoba{suffix}"] = round(float(xwoba_vals.mean()), 3)

    # DOC — Damage On Contact: avg EV on middle-middle pitches
    # Middle zone: plate_x ∈ [-0.5, 0.5], plate_z ∈ [2.0, 3.5]
    if "plate_x" in df.columns and "plate_z" in df.columns:
        mm = df[df["plate_x"].between(-0.5, 0.5) & df["plate_z"].between(2.0, 3.5)]
        mm_ev = mm["launch_speed"].dropna() if not mm.empty else pd.Series([], dtype=float)
        stats[f"doc_avg_ev{suffix}"] = round(float(mm_ev.mean()), 1) if not mm_ev.empty else None
        stats[f"doc_pitches{suffix}"] = len(mm)

    return stats


def get_batter_statcast_summary(player_id, date_str, windows=(7, 14, 30)):
    """
    Returns a flat dict of Statcast metrics across rolling windows.
    Keys are suffixed with _{n}d (e.g. avg_ev_7d, xwoba_14d).
    """
    end = datetime.strptime(date_str, "%Y-%m-%d")
    start_30 = end - timedelta(days=30)

    df = _fetch(player_id, start_30.strftime("%Y-%m-%d"), (end - timedelta(days=1)).strftime("%Y-%m-%d"))
    if df.empty:
        return {}

    if "game_date" in df.columns:
        df["game_date"] = pd.to_datetime(df["game_date"])

    result = {}
    for w in windows:
        cutoff = end - timedelta(days=w)
        if "game_date" in df.columns:
            slice_df = df[df["game_date"] >= cutoff].copy()
        else:
            slice_df = df.copy()
        result.update(_ev_stats(slice_df, suffix=f"_{w}d"))

    return result
