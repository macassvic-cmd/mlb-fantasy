"""
Optimized Slip Builder
Constructs ready-to-play parlay slips by scoring each leg with empirical
win rates from the 2940-play analysis, then greedily selecting the
combination that maximizes combined win probability.

Slip types:
  ud_8  — Underdog 8-pick  (UD lines)
  ud_6  — Underdog 6-pick  (UD lines, no player reuse from ud_8)
  pp_6  — PrizePicks 6-pick (PP lines)
  pp_4  — PrizePicks 4-pick (PP lines, no player reuse from pp_6)
"""
import json
import os

SLIPS_DIR = os.path.join("data", "slips")

# ---------------------------------------------------------------------------
# Empirical per-edge-bucket win rates — 2940 plays / 22 dates
# ---------------------------------------------------------------------------
# Edge abs bucket -> win rate for OVER calls
OVER_EDGE_WIN_RATES = [
    (2.0, 0.542),  # 2.0+ (max clamp)
    (1.5, 0.525),  # 1.5-2.0
    (1.0, 0.568),  # 1.0-1.5  ← best OVER bucket
    (0.5, 0.492),  # 0.5-1.0
    (0.0, 0.505),  # 0-0.5
]
# Edge abs bucket -> win rate for UNDER calls
UNDER_EDGE_WIN_RATES = [
    (2.0, 0.533),  # 2.0+ (max clamp)
    (1.5, 0.593),  # 1.5-2.0  ← best UNDER bucket
    (1.0, 0.470),  # 1.0-1.5  (historically bad, excluded by MIN_EDGE_UNDER)
    (0.0, 0.490),  # 0-1.0
]

MIN_EDGE_OVER  = 1.0   # mirrors VALUE_PLAY_OVER_EDGE
MIN_EDGE_UNDER = 1.5   # mirrors VALUE_PLAY_UNDER_EDGE
MIN_LEG_PROB   = 0.53  # minimum per-leg probability to be included in any slip

# ---------------------------------------------------------------------------
# Venue win-rate adjustments  (delta from 52.1% baseline)
# Best venues from the analysis: Dodger Stadium 70.8%, Kauffman 63.6%,
#   Sutter Health 62.4%, Guaranteed Rate (CWS) ~62%
# Worst: Oracle Park 40.0%, Citi Field 46.9%, Chase Field 47.4%
# ---------------------------------------------------------------------------
VENUE_WIN_ADJ = {
    "UNIQLO Field at Dodger Stadium": +0.04,
    "Kauffman Stadium":               +0.03,
    "Sutter Health Park":             +0.02,
    "Guaranteed Rate Field":          +0.02,
    "Coors Field":                    +0.02,
    "T-Mobile Park":                  +0.02,
    "Tropicana Field":                +0.015,
    "Oracle Park":                    -0.05,
    "Citi Field":                     -0.03,
    "Chase Field":                    -0.025,
    "PNC Park":                       -0.02,
    "Comerica Park":                  -0.02,
    "Rogers Centre":                  -0.02,
    "Fenway Park":                    -0.02,
}

# Team win-rate adjustments (applies to all games, home and away)
# Best: White Sox 62.7%, Rockies 61.0%, Mariners 60.3%, Braves/Rays 60.0%
# Worst: Giants 35.8%, Brewers 41.7%, Guardians 43.2%, Mets 44.0%, Nationals 45.9%
TEAM_WIN_ADJ = {
    "Chicago White Sox":    +0.03,
    "Colorado Rockies":     +0.025,
    "Seattle Mariners":     +0.02,
    "Atlanta Braves":       +0.02,
    "Tampa Bay Rays":       +0.02,
    "San Francisco Giants": -0.05,
    "Milwaukee Brewers":    -0.03,
    "Cleveland Guardians":  -0.02,
    "New York Mets":        -0.02,
    "Washington Nationals": -0.02,
}

SLIP_LABELS = {
    "ud_8": "UD 8-Pick",
    "ud_6": "UD 6-Pick",
    "pp_6": "PP 6-Pick",
    "pp_4": "PP 4-Pick",
}


# ---------------------------------------------------------------------------
# Per-leg scoring
# ---------------------------------------------------------------------------

def _edge_base_prob(edge_abs, call):
    table = OVER_EDGE_WIN_RATES if call == "over" else UNDER_EDGE_WIN_RATES
    for min_edge, rate in table:
        if edge_abs >= min_edge:
            return rate
    return 0.50


def leg_win_prob(row, call, edge_abs):
    """Empirical per-leg win probability with venue / team / situation boosts."""
    base = _edge_base_prob(edge_abs, call)
    base += VENUE_WIN_ADJ.get(row.get("venue", ""), 0)
    base += TEAM_WIN_ADJ.get(row.get("team", ""), 0)
    # Batting order 7-9: lower UD/PP lines → easier for projection to clear
    if call == "over" and (row.get("order") or 0) >= 7:
        base += 0.015
    # Platoon advantage adds confidence on OVER calls
    if row.get("platoon_advantage") == "batter" and call == "over":
        base += 0.015
    return round(min(0.95, max(0.50, base)), 4)


def _bad_era_for_under(row):
    era = row.get("opp_era")
    return era is not None and 4.5 <= era < 5.5


# ---------------------------------------------------------------------------
# Candidate generation per platform
# ---------------------------------------------------------------------------

def _ud_candidates(rows):
    """All eligible UD legs scored and sorted by win probability desc."""
    out = []
    for row in rows:
        if not row.get("market_anchored"):
            continue
        edge = row.get("edge") or 0
        if edge >= MIN_EDGE_OVER:
            call, edge_abs = "over", edge
        elif edge <= -MIN_EDGE_UNDER:
            if _bad_era_for_under(row):
                continue
            call, edge_abs = "under", abs(edge)
        else:
            continue
        prob = leg_win_prob(row, call, edge_abs)
        if prob < MIN_LEG_PROB:
            continue
        out.append(_leg_dict(row, "ud", call, row.get("ud_line"), edge, prob))
    return sorted(out, key=lambda x: x["win_prob"], reverse=True)


def _pp_candidates(rows):
    """All eligible PP legs scored and sorted by win probability desc."""
    out = []
    for row in rows:
        pp_line = row.get("pp_line")
        pp_pts  = row.get("pp_pts")
        if pp_line is None or pp_pts is None:
            continue
        pp_edge = pp_pts - pp_line
        edge_abs = min(abs(pp_edge), 2.0)
        if pp_edge >= MIN_EDGE_OVER:
            call = "over"
        elif pp_edge <= -MIN_EDGE_UNDER:
            if _bad_era_for_under(row):
                continue
            call = "under"
        else:
            continue
        prob = leg_win_prob(row, call, edge_abs)
        if prob < MIN_LEG_PROB:
            continue
        out.append(_leg_dict(row, "pp", call, pp_line, pp_edge, prob))
    return sorted(out, key=lambda x: x["win_prob"], reverse=True)


def _leg_dict(row, platform, call, line, edge, win_prob):
    return {
        "player_id":    row["player_id"],
        "name":         row["name"],
        "team":         row["team"],
        "venue":        row.get("venue", ""),
        "platform":     platform,
        "call":         call,
        "line":         round(line, 1) if line is not None else None,
        "edge":         round(edge, 2),
        "win_prob":     win_prob,
        "game_time_pt": row.get("game_time_pt"),
        "game_date_utc": row.get("game_date_utc"),
        "opp_sp":       row.get("opp_sp", ""),
        "opp_era":      row.get("opp_era"),
    }


# ---------------------------------------------------------------------------
# Slip assembly
# ---------------------------------------------------------------------------

def _build_one_slip(candidates, size, used_ids):
    """Greedy selection of `size` legs, max 3 per team, no reuse of used_ids.
    Returns slip dict or None if insufficient qualifying candidates."""
    legs = []
    team_count = {}
    for c in candidates:
        if len(legs) >= size:
            break
        if c["player_id"] in used_ids:
            continue
        if team_count.get(c["team"], 0) >= 3:
            continue
        legs.append(c)
        used_ids.add(c["player_id"])
        team_count[c["team"]] = team_count.get(c["team"], 0) + 1

    if len(legs) < size:
        return None

    combined = 1.0
    for leg in legs:
        combined *= leg["win_prob"]
    return {"legs": legs, "combined_prob": round(combined, 6), "size": size}


def build_all_slips(rows):
    """Build all 4 slip types. Returns dict with keys ud_8, ud_6, pp_6, pp_4."""
    ud_cands = _ud_candidates(rows)
    pp_cands = _pp_candidates(rows)

    ud_used, pp_used = set(), set()
    return {
        "ud_8": _build_one_slip(ud_cands, 8, ud_used),
        "ud_6": _build_one_slip(ud_cands, 6, ud_used),
        "pp_6": _build_one_slip(pp_cands, 6, pp_used),
        "pp_4": _build_one_slip(pp_cands, 4, pp_used),
    }


def save_slips(date_str, slips):
    os.makedirs(SLIPS_DIR, exist_ok=True)
    out_path = os.path.join(SLIPS_DIR, f"{date_str}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "slips": slips}, f, indent=2)
    return out_path
