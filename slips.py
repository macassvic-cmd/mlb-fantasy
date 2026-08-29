"""
Optimized Slip Builder
Constructs ready-to-play parlay slips by scoring each leg with empirical
win rates from the 2940-play analysis, then building diverse sets of slips.

Slip types:
  ud_8  — Underdog 8-pick  ×3 diverse slips
  ud_6  — Underdog 6-pick  ×3 diverse slips
  pp_6  — PrizePicks 6-pick ×3-5 diverse slips (as many as pool supports)

Diversification: each slip must share ≤ floor(size/2) legs with every other
slip of the same type. A greedy constrained builder with feasibility lookahead
selects the highest-probability valid combination at each step.
"""
import json
import os
from datetime import datetime, timezone

SLIPS_DIR = os.path.join("data", "slips")

SLIP_TYPES = {
    "ud_8": {"platform": "ud", "size": 8, "max_count": 3},
    "ud_6": {"platform": "ud", "size": 6, "max_count": 3},
    "pp_6": {"platform": "pp", "size": 6, "max_count": 5},
}

# ---------------------------------------------------------------------------
# Empirical per-edge-bucket win rates
#
# These are no longer hand-maintained. tracker.py calls
# refresh_edge_bucket_rates() nightly (see that function's docstring for the
# staleness gate) to recompute every bucket from ALL graded UD plays in
# data/results/all_results.json and persist it to EDGE_BUCKET_RATES_PATH.
# The constants below are only the cold-start fallback — used for any
# bucket the generated file doesn't have yet, or hasn't accumulated
# MIN_BUCKET_N graded plays for. Last hand-computed 2026-07-13 from the
# clean 6/14+ dataset (4,071 plays / 26 dates, post 6/10-6/13 exclusion).
# ---------------------------------------------------------------------------
EDGE_BUCKET_RATES_PATH = os.path.join("data", "results", "edge_bucket_rates.json")
EDGE_BUCKET_REFRESH_DAYS = 7
MIN_BUCKET_N = 20  # below this, a bucket is too noisy to trust — keep the fallback

_DEFAULT_OVER_EDGE_WIN_RATES = [
    (2.0, 0.505),  # 2.0+ (max clamp)
    (1.5, 0.495),  # 1.5-2.0
    (1.0, 0.543),  # 1.0-1.5  ← best OVER bucket
    (0.5, 0.495),  # 0.5-1.0
    (0.0, 0.494),  # 0-0.5
]
_DEFAULT_UNDER_EDGE_WIN_RATES = [
    (2.0, 0.526),  # 2.0+ (max clamp)
    (1.5, 0.594),  # 1.5-2.0  ← best UNDER bucket
    (1.0, 0.516),  # 1.0-1.5
    (0.0, 0.513),  # 0-1.0
]


def _merge_with_fallback(defaults, fresh_buckets):
    """fresh_buckets is a list of [min_edge, rate, n] from the generated
    file. Use the fresh rate only where n >= MIN_BUCKET_N; otherwise fall
    back to the hand-computed default for that bucket."""
    fresh_by_min = {b[0]: (b[1], b[2]) for b in fresh_buckets}
    out = []
    for min_edge, default_rate in defaults:
        rate, n = fresh_by_min.get(min_edge, (None, 0))
        out.append((min_edge, rate if (rate is not None and n >= MIN_BUCKET_N) else default_rate))
    return out


def _load_edge_bucket_rates():
    if os.path.exists(EDGE_BUCKET_RATES_PATH):
        try:
            with open(EDGE_BUCKET_RATES_PATH, encoding="utf-8") as f:
                data = json.load(f)
            over = _merge_with_fallback(_DEFAULT_OVER_EDGE_WIN_RATES, data.get("over", []))
            under = _merge_with_fallback(_DEFAULT_UNDER_EDGE_WIN_RATES, data.get("under", []))
            return over, under
        except Exception:
            pass
    return list(_DEFAULT_OVER_EDGE_WIN_RATES), list(_DEFAULT_UNDER_EDGE_WIN_RATES)


OVER_EDGE_WIN_RATES, UNDER_EDGE_WIN_RATES = _load_edge_bucket_rates()


def _edge_bucket_stats(results_data):
    """Compute fresh win rates per (direction, edge-bucket) from every
    graded UD play in results_data (data/results/all_results.json), using
    the same bucket boundaries as the *_EDGE_WIN_RATES tables. Returns
    (over_buckets, under_buckets), each a list of [min_edge, rate_or_None, n]."""
    over_bounds = [b[0] for b in _DEFAULT_OVER_EDGE_WIN_RATES]
    under_bounds = [b[0] for b in _DEFAULT_UNDER_EDGE_WIN_RATES]

    def bucket_for(edge_abs, bounds):
        for b in bounds:
            if edge_abs >= b:
                return b
        return bounds[-1]

    over_counts = {b: [0, 0] for b in over_bounds}   # min_edge -> [wins, total]
    under_counts = {b: [0, 0] for b in under_bounds}

    for p in results_data.get("players", {}).values():
        for h in p.get("history", []):
            if h.get("result_ud") not in ("win", "loss"):
                continue
            line = h.get("ud_line")
            proj = h.get("projected_ud")
            if line is None or proj is None:
                continue
            edge = proj - line
            if edge == 0:
                continue
            call = "over" if edge > 0 else "under"
            edge_abs = abs(edge)
            counts = over_counts if call == "over" else under_counts
            bounds = over_bounds if call == "over" else under_bounds
            b = bucket_for(edge_abs, bounds)
            counts[b][1] += 1
            if h["result_ud"] == "win":
                counts[b][0] += 1

    def to_list(counts, bounds):
        out = []
        for b in bounds:
            wins, total = counts[b]
            rate = round(wins / total, 4) if total else None
            out.append([b, rate, total])
        return out

    return to_list(over_counts, over_bounds), to_list(under_counts, under_bounds)


def refresh_edge_bucket_rates(results_data, force=False):
    """Recompute OVER/UNDER edge-bucket win rates from all graded UD plays
    and persist to EDGE_BUCKET_RATES_PATH, at most once every
    EDGE_BUCKET_REFRESH_DAYS days unless force=True. Called nightly by
    tracker.py; this keeps the table from ever going stale again without a
    manual update. Buckets below MIN_BUCKET_N graded plays fall back to the
    hand-computed defaults at load time (see _merge_with_fallback) rather
    than let noise drive slip construction."""
    if not force and os.path.exists(EDGE_BUCKET_RATES_PATH):
        try:
            with open(EDGE_BUCKET_RATES_PATH, encoding="utf-8") as f:
                existing = json.load(f)
            last = existing.get("computed_at")
            if last:
                age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days
                if age_days < EDGE_BUCKET_REFRESH_DAYS:
                    return existing
        except Exception:
            pass

    over, under = _edge_bucket_stats(results_data)
    payload = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "over": over,
        "under": under,
    }
    os.makedirs(os.path.dirname(EDGE_BUCKET_RATES_PATH), exist_ok=True)
    with open(EDGE_BUCKET_RATES_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return payload


# ---------------------------------------------------------------------------
# PP-specific edge-bucket calibration
#
# 2026-07-21: built after confirming _edge_bucket_stats() above is UD-only
# (reads result_ud/ud_line/projected_ud) yet was being applied to PP edges
# anyway - live gap was -13.5pt (predicted 57.8%, actual 44.3%). PP's own
# real-line calibration is genuinely different in shape, not just worse:
# e.g. UNDER 1.5-2.0 is UD's *best* bucket (59.4%) but PP's *weakest*
# (48.2%). Fit from 2026-06-12 to 2026-07-07 (21 dates with real PrizePicks
# lines). PrizePicks returned zero lines every day from 2026-07-08 until
# the fetch itself was removed 2026-08-29 (DataDome bot-protection never
# lifted - see scrapers/market_lines.py's module docstring), so this
# can't be refreshed with live data unless PP fetching is reinstated. NOT
# wired into _pp_candidates() yet - PP stays paused regardless of calibration quality
# until there's a live source of real PP lines again; this exists so
# re-enabling PP is a calibration swap, not a re-investigation.
# ---------------------------------------------------------------------------
PP_EDGE_BUCKET_RATES_PATH = os.path.join("data", "results", "pp_edge_bucket_rates.json")

_DEFAULT_PP_OVER_EDGE_WIN_RATES = [
    (2.0, 0.560),
    (1.5, 0.570),
    (1.0, 0.516),
    (0.5, 0.547),
    (0.0, 0.524),
]
_DEFAULT_PP_UNDER_EDGE_WIN_RATES = [
    (2.0, 0.589),
    (1.5, 0.482),
    (1.0, 0.486),
    (0.0, 0.507),
]


def _is_real_pp_line_date(date_str):
    """True if data/market_lines/<date>.json shows a real (non-broken) PP
    fetch that day - i.e. enough posted lines that they weren't all derived
    from the UD line via the fallback ratio (see match_lines)."""
    path = os.path.join("data", "market_lines", f"{date_str}.json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return len(data.get("pp", {})) >= 50
    except Exception:
        return False


def _pp_edge_bucket_stats(results_data):
    """Same as _edge_bucket_stats but for PP (result_pp/pp_line/
    projected_pp), restricted to dates with a real PP line fetch that day -
    see _is_real_pp_line_date. Returns (over_buckets, under_buckets), each
    a list of [min_edge, rate_or_None, n]."""
    over_bounds = [b[0] for b in _DEFAULT_PP_OVER_EDGE_WIN_RATES]
    under_bounds = [b[0] for b in _DEFAULT_PP_UNDER_EDGE_WIN_RATES]

    def bucket_for(edge_abs, bounds):
        for b in bounds:
            if edge_abs >= b:
                return b
        return bounds[-1]

    over_counts = {b: [0, 0] for b in over_bounds}
    under_counts = {b: [0, 0] for b in under_bounds}
    date_cache = {}

    for p in results_data.get("players", {}).values():
        for h in p.get("history", []):
            if h.get("result_pp") not in ("win", "loss"):
                continue
            date = h.get("date")
            if date not in date_cache:
                date_cache[date] = _is_real_pp_line_date(date)
            if not date_cache[date]:
                continue
            line = h.get("pp_line")
            proj = h.get("projected_pp")
            if line is None or proj is None:
                continue
            edge = proj - line
            if edge == 0:
                continue
            call = "over" if edge > 0 else "under"
            edge_abs = min(abs(edge), 2.0)
            counts = over_counts if call == "over" else under_counts
            bounds = over_bounds if call == "over" else under_bounds
            b = bucket_for(edge_abs, bounds)
            counts[b][1] += 1
            if h["result_pp"] == "win":
                counts[b][0] += 1

    def to_list(counts, bounds):
        out = []
        for b in bounds:
            wins, total = counts[b]
            rate = round(wins / total, 4) if total else None
            out.append([b, rate, total])
        return out

    return to_list(over_counts, over_bounds), to_list(under_counts, under_bounds)


def refresh_pp_edge_bucket_rates(results_data, force=False):
    """Recompute PP-specific OVER/UNDER edge-bucket win rates and persist
    to PP_EDGE_BUCKET_RATES_PATH. Mirrors refresh_edge_bucket_rates()'s
    staleness gate, but in practice this will keep returning the same
    2026-06-12/2026-07-07 fit until PrizePicks scraping is restored, since
    _is_real_pp_line_date() excludes every date after that."""
    if not force and os.path.exists(PP_EDGE_BUCKET_RATES_PATH):
        try:
            with open(PP_EDGE_BUCKET_RATES_PATH, encoding="utf-8") as f:
                existing = json.load(f)
            last = existing.get("computed_at")
            if last:
                age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days
                if age_days < EDGE_BUCKET_REFRESH_DAYS:
                    return existing
        except Exception:
            pass

    over, under = _pp_edge_bucket_stats(results_data)
    payload = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "over": over,
        "under": under,
    }
    os.makedirs(os.path.dirname(PP_EDGE_BUCKET_RATES_PATH), exist_ok=True)
    with open(PP_EDGE_BUCKET_RATES_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return payload


MIN_LEG_PROB   = 0.53

# ---------------------------------------------------------------------------
# VENUE_WIN_ADJ / TEAM_WIN_ADJ are historical reference only as of 2026-08-18
# - their sole consumer, leg_win_prob(), has had no live caller since the
# 2026-07-21 rebuild moved UD candidates onto premium_tier()'s own rate
# directly (see the "Candidate generation" comment below), and premium_tier
# itself is now retired (see PREMIUM_TIER_REINCLUSION_MIN_N above). Kept
# accurate anyway per "kept but unwired, preserves the record" - cheap to
# maintain and useful if either tier or a general edge-bucket model is ever
# reinstated.
#
# 2026-08-18 full recalibration, second pass after discovering the local
# repo was 13 dates stale (61 dates, 2026-06-14 to 2026-08-17, 8,193 graded
# plays; baseline 50.7%). adj = (fresh win rate - baseline) * 0.007, rounded
# to nearest 0.005; dropped if |delta| < 3.0pt or n < 150. Numbers moved
# again from the first (48-date) pass: Kauffman Stadium (+5.3pt -> +2.5pt),
# Tropicana Field (+3.2pt -> +2.0pt) and Rogers Centre (-3.4pt -> -2.2pt)
# all fell below the keep bar with more data. T-Mobile Park flipped instead
# of dropping further: first pass read it as flat (-0.4pt, dropped), full
# data shows a real -4.0pt deficit (n=199) - the opposite of its ORIGINAL
# pre-recalibration value (+0.02, i.e. thought to be a good venue).
# ---------------------------------------------------------------------------
VENUE_WIN_ADJ = {
    "Sutter Health Park": +0.04,
    "Oracle Park":        -0.04,
    "Citi Field":         -0.03,
    "Chase Field":        -0.02,
    "T-Mobile Park":      -0.03,
}

# Same 61-date recompute as above. Colorado Rockies (+4.7pt -> +2.8pt),
# Tampa Bay Rays (+3.2pt -> +2.6pt) and Washington Nationals (-4.0pt ->
# -0.6pt) all fell below the keep bar with more data - Washington Nationals
# in particular looked like the most stable entry after the first pass and
# didn't survive a second round. Two CONFIRMED sign flips from the original
# pre-recalibration values, in the same direction on both the partial and
# full recompute: Cleveland Guardians (orig -0.02 -> +1.5pt partial ->
# +3.1pt full, n=221) and Seattle Mariners (orig +0.02 -> -2.4pt partial ->
# -5.5pt full, n=254) - worth flagging to a human before ever trusting a
# reversal like that operationally, even with two passes agreeing.
TEAM_WIN_ADJ = {
    "Atlanta Braves":      +0.02,
    "Cleveland Guardians": +0.02,
    "Seattle Mariners":    -0.04,
}


# ---------------------------------------------------------------------------
# Premium tier — subset-mining results from the 2026-07-13 All-Star-break
# analysis (4,071 graded UD plays, 2026-06-14 to 2026-07-11). Overall model
# accuracy was flat at 51-53%, so instead of chasing broad accuracy this
# isolates the specific subsets that already win big, for a dashboard
# "Premium" section (see report.py) surfaced above Value Plays.
# ---------------------------------------------------------------------------

# Tier B's team list — top single-dimension team subsets from the mining
# pass (57.6-60.0% each alone), used below only in combination with
# batting order 3-4.
PREMIUM_TOP_TEAMS = {
    "St. Louis Cardinals", "Tampa Bay Rays", "Colorado Rockies",
    "Pittsburgh Pirates", "Atlanta Braves", "Boston Red Sox",
}
PREMIUM_VENUE = "Citizens Bank Park"


def _premium_tier_a(row, call, edge_abs):
    """UNDER call + edge 1.5-1.99 + batting order 3-4. The single best
    combo found: 67.8% win rate (87 plays / 23 of 26 dates), stable across
    both halves of the sample (H1 74.4% / H2 61.4%). order_bucket 3-4 is
    weak alone (53.3%) but replicated as a multiplier across five
    independent team subsets in the same analysis."""
    return call == "under" and 1.5 <= edge_abs < 2.0 and (row.get("order") or 0) in (3, 4)


def _premium_tier_b(row, call, edge_abs):
    """Tier A broadened with two more validated subsets: PREMIUM_TOP_TEAMS
    batting 3rd/4th, and any play at Citizens Bank Park (60.2% alone, n=88).
    Backtested 64.1% union win rate (373 plays / 26 dates), stable both
    halves (H1 66.5% / H2 62.2%). Checked independently of Tier A — callers
    should check Tier A first so a play is only ever badged once."""
    if row.get("team") in PREMIUM_TOP_TEAMS and (row.get("order") or 0) in (3, 4):
        return True
    if row.get("venue") == PREMIUM_VENUE:
        return True
    return False


def premium_tier(row):
    """Classify a market-anchored dashboard row as 'premium', 'strong', or
    None. Expects the report.py row shape (edge/order/team/venue keys).
    Returns None for rows without a resolved non-zero edge."""
    edge = row.get("edge")
    if not edge:
        return None
    call = "over" if edge > 0 else "under"
    edge_abs = abs(edge)
    if _premium_tier_a(row, call, edge_abs):
        return "premium"
    if _premium_tier_b(row, call, edge_abs):
        return "strong"
    return None


# ---------------------------------------------------------------------------
# Strong tier (Tier B) reinclusion gate
#
# 2026-07-21: excluded from slip construction after its slip-subset showed
# 35.5% live (n=31) vs a 64.1% backtest. The broader dashboard-wide record
# (grade_premium_plays, all Strong-tier calls not just the slip subset) is
# 53.7% (n=67) as of the same date - still below both the backtest and the
# ~55% breakeven a slip needs, but not as dire as the narrower slip subset
# suggested. Track it here explicitly rather than leaving the "does it earn
# back in" decision as something that has to be recomputed by hand.
# ---------------------------------------------------------------------------
STRONG_TIER_REINCLUSION_MIN_N = 50
STRONG_TIER_REINCLUSION_MIN_RATE = 0.58


def strong_tier_reinclusion_status(record):
    """record: {"wins": int, "losses": int} from premium_results.json's
    record["strong"]. Returns (eligible: bool, status_str)."""
    wins, losses = record.get("wins", 0), record.get("losses", 0)
    n = wins + losses
    rate = wins / n if n else 0.0
    if n < STRONG_TIER_REINCLUSION_MIN_N:
        return False, (f"Strong tier: {wins}-{losses} ({rate*100:.1f}%, n={n}) - "
                        f"needs {STRONG_TIER_REINCLUSION_MIN_N}+ plays before reconsidering "
                        f"({STRONG_TIER_REINCLUSION_MIN_N - n} more needed)")
    if rate >= STRONG_TIER_REINCLUSION_MIN_RATE:
        return True, (f"Strong tier: {wins}-{losses} ({rate*100:.1f}%, n={n}) - "
                       f"clears the {STRONG_TIER_REINCLUSION_MIN_RATE*100:.0f}% reinclusion bar, "
                       f"eligible to revisit at a discount")
    return False, (f"Strong tier: {wins}-{losses} ({rate*100:.1f}%, n={n}) - "
                    f"below the {STRONG_TIER_REINCLUSION_MIN_RATE*100:.0f}% reinclusion bar, stays out")


# ---------------------------------------------------------------------------
# Premium tier (Tier A) reinclusion gate - kept for reference only. The
# Premium/Strong tiers, Slips, and Stacks were all retired from the live
# dashboard on 2026-08-18 (see report.py/tracker.py); premium_tier() and
# this gate are no longer called from anywhere live.
#
# Final numbers before retirement, re-run after discovering the local repo
# was 13 dates stale (61 dates, 2026-06-14 to 2026-08-17): Tier A's
# 2026-07-13 backtest (67.8%/67.4% on rejoin, n=87-92, discovery window
# 2026-06-14 to 2026-07-12) never held out of sample. Full out-of-sample
# record (matching premium_results.json's record["premium"] exactly) is
# 42-42 (50.0%, n=84) - a dead-flat coin flip across three separate ~9-42
# play windows (33.3%, 52.4%, 51.5%), not a blip. Combined discovery+OOS is
# 59.1% (n=176), still above 50% but eroding toward it as OOS data
# accumulates and dilutes the in-sample half. This is what "leave it
# excluded rather than search for a replacement" was resolving before the
# broader decision to retire Premium/Slips/Stacks entirely superseded it.
# ---------------------------------------------------------------------------
PREMIUM_TIER_REINCLUSION_MIN_N = 50
PREMIUM_TIER_REINCLUSION_MIN_RATE = 0.58


def premium_tier_reinclusion_status(record):
    """record: {"wins": int, "losses": int} from premium_results.json's
    record["premium"]. Returns (eligible: bool, status_str)."""
    wins, losses = record.get("wins", 0), record.get("losses", 0)
    n = wins + losses
    rate = wins / n if n else 0.0
    if n < PREMIUM_TIER_REINCLUSION_MIN_N:
        return False, (f"Premium tier: {wins}-{losses} ({rate*100:.1f}%, n={n}) - "
                        f"needs {PREMIUM_TIER_REINCLUSION_MIN_N}+ plays before reconsidering "
                        f"({PREMIUM_TIER_REINCLUSION_MIN_N - n} more needed)")
    if rate >= PREMIUM_TIER_REINCLUSION_MIN_RATE:
        return True, (f"Premium tier: {wins}-{losses} ({rate*100:.1f}%, n={n}) - "
                       f"clears the {PREMIUM_TIER_REINCLUSION_MIN_RATE*100:.0f}% reinclusion bar, "
                       f"eligible to revisit at a discount")
    return False, (f"Premium tier: {wins}-{losses} ({rate*100:.1f}%, n={n}) - "
                    f"below the {PREMIUM_TIER_REINCLUSION_MIN_RATE*100:.0f}% reinclusion bar, stays out")


# ---------------------------------------------------------------------------
# Per-leg scoring
# ---------------------------------------------------------------------------

def _edge_base_prob(edge_abs, call):
    table = OVER_EDGE_WIN_RATES if call == "over" else UNDER_EDGE_WIN_RATES
    for min_edge, rate in table:
        if edge_abs >= min_edge:
            return rate
    return 0.50


# Reliability check (2026-07-13, 4,071 graded plays / 26 dates) found
# leg_win_prob() well calibrated at the extremes but overconfident in the
# middle of its range:
#   predicted 50-53%: actual 49.7% (-0.9 pt — within noise)
#   predicted 53-55%: actual 50.7% (-3.2 pt)
#   predicted 55-58%: actual 53.9% (-2.5 pt)
#   predicted 58%+:   actual 58.4% (-1.5 pt — essentially honest)
# Shrink the two overconfident mid-bands by their measured gap so slips and
# Value Plays stop quietly including inflated middle-band plays.
_CALIBRATION_CORRECTION = [
    (0.53, 0.55, 0.032),
    (0.55, 0.58, 0.025),
]


def _apply_calibration_correction(prob):
    for lo, hi, shrink in _CALIBRATION_CORRECTION:
        if lo <= prob < hi:
            return prob - shrink
    return prob


def leg_win_prob(row, call, edge_abs):
    base = _edge_base_prob(edge_abs, call)
    base += VENUE_WIN_ADJ.get(row.get("venue", ""), 0)
    base += TEAM_WIN_ADJ.get(row.get("team", ""), 0)
    if call == "over" and (row.get("order") or 0) >= 7:
        base += 0.015
    if row.get("platoon_advantage") == "batter" and call == "over":
        base += 0.015
    base = min(0.95, max(0.50, base))
    base = _apply_calibration_correction(base)
    return round(min(0.95, max(0.50, base)), 4)


def _bad_era_for_under(row):
    era = row.get("opp_era")
    return era is not None and 4.5 <= era < 5.5


# ---------------------------------------------------------------------------
# Candidate generation
#
# 2026-07-21 rebuild: the 0-100 live slip record (UD legs 52.5%, PP legs
# 44.4% — both at or below the ~55% breakeven a 6/8-pick multiplier needs)
# traced to candidates being drawn from the whole market_anchored pool
# gated only by the generic edge-bucket leg_win_prob() >= 0.53 — i.e. the
# general leaderboard, not premium_tier(). At the time, Tier A ("premium")
# checked out as the one validated signal (68.1% live, n=47, matching its
# 67.8% backtest) and became the sole UD candidate source, with Tier B
# ("strong") excluded pending re-validation (see strong_tier_reinclusion_status).
#
# 2026-08-18 recalibration: Tier A itself failed the same test it was kept
# for - full out-of-sample record 42-42 (50.0%, n=84), see the reinclusion
# gate comment above. With Tier A excluded and Tier B still excluded, there
# was no validated UD signal left to draw candidates from, and Slips itself
# was retired from the live dashboard the same day (see report.py) - this
# function has had no caller since.
# ---------------------------------------------------------------------------


def _leg_dict(row, platform, call, line, edge, win_prob):
    return {
        "player_id":     row["player_id"],
        "name":          row["name"],
        "team":          row["team"],
        "venue":         row.get("venue", ""),
        "platform":      platform,
        "call":          call,
        "line":          round(line, 1) if line is not None else None,
        "edge":          round(edge, 2),
        "win_prob":      win_prob,
        "game_time_pt":  row.get("game_time_pt"),
        "game_date_utc": row.get("game_date_utc"),
        "opp_sp":        row.get("opp_sp", ""),
        "opp_era":       row.get("opp_era"),
    }


def _ud_candidates(rows):
    # Unused since Slips was retired from the live dashboard 2026-08-18 (see
    # report.py - it no longer calls build_all_slips at all). Left returning
    # [] rather than deleted: Tier A ("premium"), the sole source here, had
    # already collapsed to a 50.0% out-of-sample record (n=84) before the
    # broader retirement decision superseded that question. Re-enable via
    # report.py once a validated UD signal exists again.
    return []


def _pp_candidates(rows):
    # Paused 2026-07-21: PP has no validated high-confidence signal. The
    # UD-mined premium_tier() rates don't transfer (PP "premium" legs hit
    # 41.4% live, n=29 — nowhere near UD's 68.1%), and the edge-bucket
    # leg_win_prob() model is fit exclusively on UD outcomes
    # (_edge_bucket_stats reads only result_ud/ud_line/projected_ud) yet
    # was being applied to PP's edge values anyway — average predicted
    # 57.8% vs actual 44.3% (-13.5pt) live. Shipping PP slips on either
    # foundation repeats the mistake that produced the 0-44 PP record.
    # Re-enable once PP has its own edge-bucket calibration built from
    # PP's own graded history (mirroring refresh_edge_bucket_rates) with
    # enough graded plays to trust it.
    return []


# ---------------------------------------------------------------------------
# Diversified slip builder
# ---------------------------------------------------------------------------

def _reorder_for_diversity(candidates, existing_slips):
    """Put candidates not in any existing slip first, preserving win_prob order within each group."""
    if not existing_slips:
        return candidates
    all_existing_ids = set()
    for s in existing_slips:
        all_existing_ids.update(l["player_id"] for l in s["legs"])
    fresh = [c for c in candidates if c["player_id"] not in all_existing_ids]
    shared = [c for c in candidates if c["player_id"] in all_existing_ids]
    return fresh + shared


def _build_constrained(candidates, size, existing_slips, rank):
    """Greedy slip with max floor(size/2) overlap with each existing slip.
    Feasibility lookahead prevents greedy from painting itself into a corner."""
    max_overlap = size // 2
    existing_id_sets = [set(l["player_id"] for l in s["legs"]) for s in existing_slips]
    ordered = _reorder_for_diversity(candidates, existing_slips)

    legs = []
    picked_ids = set()
    team_count = {}
    overlaps = [0] * len(existing_id_sets)

    for idx, c in enumerate(ordered):
        if len(legs) >= size:
            break
        pid = c["player_id"]
        if pid in picked_ids:
            continue
        if team_count.get(c["team"], 0) >= 3:
            continue

        new_overlaps = [ov + (1 if pid in ids else 0)
                        for ov, ids in zip(overlaps, existing_id_sets)]
        if any(ov > max_overlap for ov in new_overlaps):
            continue

        # Feasibility: can the remaining pool still satisfy min non-overlap from each existing slip?
        remaining = [
            c2 for c2 in ordered[idx + 1:]
            if c2["player_id"] not in picked_ids
            and team_count.get(c2["team"], 0) < 3
        ]
        feasible = True
        for i, ids in enumerate(existing_id_sets):
            non_ov_so_far = len(legs) + 1 - new_overlaps[i]
            still_need = max(0, (size - max_overlap) - non_ov_so_far)
            if still_need > 0:
                avail = sum(1 for c2 in remaining if c2["player_id"] not in ids)
                if avail < still_need:
                    feasible = False
                    break
        if not feasible:
            continue

        legs.append(c)
        picked_ids.add(pid)
        team_count[c["team"]] = team_count.get(c["team"], 0) + 1
        overlaps = new_overlaps

    if len(legs) < size:
        return None

    combined = 1.0
    for leg in legs:
        combined *= leg["win_prob"]

    timed = [(l["game_date_utc"], l.get("game_time_pt")) for l in legs if l.get("game_date_utc")]
    timed.sort()
    lock_utc, lock_pt = timed[0] if timed else (None, None)

    return {
        "legs":          legs,
        "combined_prob": round(combined, 6),
        "size":          size,
        "rank":          rank,
        "lock_utc":      lock_utc,
        "lock_pt":       lock_pt,
    }


def build_diverse_slips(candidates, size, max_count):
    """Build up to max_count diverse slips of given size."""
    slips = []
    for i in range(max_count):
        slip = _build_constrained(candidates, size, slips, rank=i + 1)
        if slip is None:
            break
        slips.append(slip)
    return slips


def build_all_slips(rows):
    """Build all slip types. Returns {ud_8: [...], ud_6: [...], pp_6: [...]}."""
    ud_cands = _ud_candidates(rows)
    pp_cands = _pp_candidates(rows)
    return {
        "ud_8": build_diverse_slips(ud_cands, 8, 3),
        "ud_6": build_diverse_slips(ud_cands, 6, 3),
        "pp_6": build_diverse_slips(pp_cands, 6, 5),
    }


def save_slips(date_str, slips):
    os.makedirs(SLIPS_DIR, exist_ok=True)
    out_path = os.path.join(SLIPS_DIR, f"{date_str}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "slips": slips}, f, indent=2)
    return out_path
