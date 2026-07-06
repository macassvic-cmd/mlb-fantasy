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

SLIPS_DIR = os.path.join("data", "slips")

SLIP_TYPES = {
    "ud_8": {"platform": "ud", "size": 8, "max_count": 3},
    "ud_6": {"platform": "ud", "size": 6, "max_count": 3},
    "pp_6": {"platform": "pp", "size": 6, "max_count": 5},
}

# ---------------------------------------------------------------------------
# Empirical per-edge-bucket win rates — 2940 plays / 22 dates
# ---------------------------------------------------------------------------
OVER_EDGE_WIN_RATES = [
    (2.0, 0.542),  # 2.0+ (max clamp)
    (1.5, 0.525),  # 1.5-2.0
    (1.0, 0.568),  # 1.0-1.5  ← best OVER bucket
    (0.5, 0.492),  # 0.5-1.0
    (0.0, 0.505),  # 0-0.5
]
UNDER_EDGE_WIN_RATES = [
    (2.0, 0.533),  # 2.0+ (max clamp)
    (1.5, 0.593),  # 1.5-2.0  ← best UNDER bucket
    (1.0, 0.470),  # 1.0-1.5  (historically bad)
    (0.0, 0.490),  # 0-1.0
]

MIN_EDGE_OVER  = 1.0
MIN_EDGE_UNDER = 1.5
MIN_LEG_PROB   = 0.53

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
    base = _edge_base_prob(edge_abs, call)
    base += VENUE_WIN_ADJ.get(row.get("venue", ""), 0)
    base += TEAM_WIN_ADJ.get(row.get("team", ""), 0)
    if call == "over" and (row.get("order") or 0) >= 7:
        base += 0.015
    if row.get("platoon_advantage") == "batter" and call == "over":
        base += 0.015
    return round(min(0.95, max(0.50, base)), 4)


def _bad_era_for_under(row):
    era = row.get("opp_era")
    return era is not None and 4.5 <= era < 5.5


# ---------------------------------------------------------------------------
# Candidate generation
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
    out = []
    for row in rows:
        if not row.get("market_anchored"):
            continue
        edge = row.get("edge") or 0
        if edge >= MIN_EDGE_OVER:
            call, edge_abs = "over", min(edge, 2.0)
        elif edge <= -MIN_EDGE_UNDER:
            if _bad_era_for_under(row):
                continue
            call, edge_abs = "under", min(abs(edge), 2.0)
        else:
            continue
        prob = leg_win_prob(row, call, edge_abs)
        if prob < MIN_LEG_PROB:
            continue
        out.append(_leg_dict(row, "ud", call, row.get("ud_line"), edge, prob))
    return sorted(out, key=lambda x: x["win_prob"], reverse=True)


def _pp_candidates(rows):
    out = []
    for row in rows:
        pp_line = row.get("pp_line")
        pp_pts  = row.get("pp_pts")
        if pp_line is None or pp_pts is None:
            continue
        pp_edge  = pp_pts - pp_line
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
