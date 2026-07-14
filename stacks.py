"""
Correlated Stack Optimizer — a 30x payout bet type: 3 players from one
team + 3 from another, each needing H+R+RBI >= 2.

Built entirely on the empirical Phase A order x opponent-ERA backtest
(2026-07-14, 5,918 player-games / 27 dates, 2026-06-14 to 2026-07-12).
A plate-appearance-level Monte Carlo simulator was also built and tuned
(ERA-exponent fit via H1/H2 holdout) but failed its own out-of-sample
joint-probability validation gate even after fixes — its 60%+ predictions
only actually hit ~51% on holdout, a ~16pt miss on a hard ~4pt bar. It's
kept in scratchpad as a research artifact, not used here. Real observed
frequencies can't be miscalibrated the way a compounding multiplicative
model can, so this module uses them directly instead.

Correlation is modeled conservatively: the raw adjacent-lineup-spot lift
(B's hit rate when teammate A, one spot up, also hit) is confounded with
general "good team, good day" variance — proven by non-adjacent control
pairs (1->5, 2->7, 3->9) showing lift too (avg +4.4pt) despite no
adjacency relationship. The adjacency-*specific* effect used for the
conservative bound is the raw lift minus that control-pair average; the
optimistic bound uses the raw (undeconfounded) lift.
"""
import json
import os
from datetime import datetime, timezone

RATES_PATH = os.path.join("data", "results", "hrrbi_rates.json")
RATES_REFRESH_DAYS = 7
MIN_CELL_N = 50

PAYOUT = 30
BREAKEVEN_PROB = 1 / PAYOUT
PLAYABLE_MIN_CONSERVATIVE = 0.045

TRIO_TYPES = [
    {"label": "1-2-3", "slots": (1, 2, 3), "pairs": ((1, 2), (2, 3))},
    {"label": "2-3-4", "slots": (2, 3, 4), "pairs": ((2, 3), (3, 4))},
]

ORDER_BUCKET_OF_SLOT = {1: "1-2", 2: "1-2", 3: "3-4", 4: "3-4",
                         5: "5-6", 6: "5-6", 7: "7-9", 8: "7-9", 9: "7-9"}

# ---------------------------------------------------------------------------
# Defaults — hand-computed 2026-07-14 from 5,918 player-games / 27 dates.
# refresh_hrrbi_rates() (called nightly by tracker.py, rate-limited weekly)
# recomputes these from every data/results/results_*.json snapshot; any
# cross-table cell below MIN_CELL_N graded plays falls back to these.
# ---------------------------------------------------------------------------
_DEFAULT_MARGINAL_ORDER_RATES = {
    "1-2": 0.526, "3-4": 0.492, "5-6": 0.433, "7-9": 0.388,
}
_DEFAULT_CROSS_TABLE = {
    # "order_bucket|era_bucket" -> [rate, n]
    "1-2|<3.50":       [0.481, 493], "1-2|3.50-4.49":   [0.543, 398],
    "1-2|4.50-5.49":   [0.537, 244], "1-2|5.50+":       [0.591, 186],
    "3-4|<3.50":       [0.456, 487], "3-4|3.50-4.49":   [0.470, 400],
    "3-4|4.50-5.49":   [0.542, 238], "3-4|5.50+":       [0.561, 189],
    "5-6|<3.50":       [0.402, 475], "5-6|3.50-4.49":   [0.449, 390],
    "5-6|4.50-5.49":   [0.462, 234], "5-6|5.50+":       [0.435, 184],
    "7-9|<3.50":       [0.350, 685], "7-9|3.50-4.49":   [0.374, 553],
    "7-9|4.50-5.49":   [0.426, 333], "7-9|5.50+":       [0.464, 248],
}
# Adjacency: baseline = spot B's own unconditional rate; raw = observed
# P(B hits 2+ | teammate A, one spot up, also hit 2+); "n" = games with
# both A and B present. Control-pair average lift (non-adjacent spots)
# used to deconfound: +4.4pt, from 1->5 (+4.5), 2->7 (+2.5), 3->9 (+6.1).
_CONTROL_LIFT_PTS = 4.4
_DEFAULT_ADJACENCY = {
    "1,2": {"baseline": 0.527, "raw": 0.565, "n": 352},
    "2,3": {"baseline": 0.483, "raw": 0.538, "n": 351},
    "3,4": {"baseline": 0.502, "raw": 0.612, "n": 320},
}


def era_bucket(era):
    if era is None:
        return None
    if era < 3.5:
        return "<3.50"
    if era < 4.5:
        return "3.50-4.49"
    if era < 5.5:
        return "4.50-5.49"
    return "5.50+"


# ---------------------------------------------------------------------------
# Rates loading (self-updating, mirrors slips.py's edge-bucket pattern)
# ---------------------------------------------------------------------------

def _load_rates():
    cross_table = dict(_DEFAULT_CROSS_TABLE)
    marginal = dict(_DEFAULT_MARGINAL_ORDER_RATES)
    adjacency = {k: dict(v) for k, v in _DEFAULT_ADJACENCY.items()}

    if os.path.exists(RATES_PATH):
        try:
            with open(RATES_PATH, encoding="utf-8") as f:
                data = json.load(f)
            for key, (rate, n) in data.get("cross_table", {}).items():
                if n >= MIN_CELL_N:
                    cross_table[key] = [rate, n]
            for key, rate in data.get("marginal_order_rates", {}).items():
                marginal[key] = rate
            for key, adj in data.get("adjacency", {}).items():
                if adj.get("n", 0) >= MIN_CELL_N:
                    adjacency[key] = adj
        except Exception:
            pass

    return cross_table, marginal, adjacency


CROSS_TABLE, MARGINAL_ORDER_RATES, ADJACENCY = _load_rates()


def leg_probability(slot, era):
    """P(H+R+RBI>=2) for a player batting in this slot against this
    opponent ERA, from the order x ERA cross-table (falling back to the
    order-bucket marginal when the specific cell is thin — see
    MIN_CELL_N)."""
    ob = ORDER_BUCKET_OF_SLOT.get(slot)
    eb = era_bucket(era)
    if ob is None:
        return None
    if eb is not None:
        cell = CROSS_TABLE.get(f"{ob}|{eb}")
        if cell is not None:
            return cell[0]
    return MARGINAL_ORDER_RATES.get(ob)


def _adjacency_ratio(pair, scenario):
    """Multiplicative conditional-probability ratio for teammate B (one
    spot below A) given A also hit 2+. scenario is 'conservative' (raw
    lift minus the control-pair confound, floored at 1.0 — never a
    penalty) or 'optimistic' (the raw, undeconfounded measurement)."""
    key = f"{pair[0]},{pair[1]}"
    adj = ADJACENCY.get(key)
    if adj is None:
        return 1.0
    baseline, raw = adj["baseline"], adj["raw"]
    if baseline <= 0:
        return 1.0
    if scenario == "optimistic":
        return raw / baseline
    deconf_lift = (raw - baseline) - (_CONTROL_LIFT_PTS / 100)
    deconf_rate = baseline + max(0.0, deconf_lift)
    return max(1.0, deconf_rate / baseline)


def trio_joint_prob(leg_probs, pairs, scenario):
    """Chain P(1st hits) * P(2nd hits | 1st) * P(3rd hits | 2nd), where
    each conditional is that player's own (order x era) base rate scaled
    by the adjacency ratio for its pair — grounds the correlation boost in
    each specific player's real matchup rather than a generic pair
    average."""
    p1, p2, p3 = leg_probs
    r_12 = _adjacency_ratio(pairs[0], scenario)
    r_23 = _adjacency_ratio(pairs[1], scenario)
    p2_cond = min(1.0, p2 * r_12)
    p3_cond = min(1.0, p3 * r_23)
    return p1 * p2_cond * p3_cond


# ---------------------------------------------------------------------------
# Trio / pairing selection
# ---------------------------------------------------------------------------

SOFT_ERA_MIN = 4.50


def find_trio_candidates(rows):
    """rows: today's dashboard rows (report.build_row output). Returns a
    list of qualifying trios: 3 consecutive top-of-order teammates (slots
    1-2-3 or 2-3-4) facing an opponent starter with ERA >= SOFT_ERA_MIN."""
    by_team_game = {}
    for r in rows:
        order = r.get("order")
        if not order or not (1 <= order <= 9):
            continue
        key = (r.get("game_pk"), r.get("team"))
        by_team_game.setdefault(key, {})[order] = r

    candidates = []
    for (game_pk, team), lineup in by_team_game.items():
        if 1 not in lineup:
            continue
        era = lineup[1].get("opp_era")
        if era is None or era < SOFT_ERA_MIN:
            continue

        for trio_type in TRIO_TYPES:
            slots = trio_type["slots"]
            if not all(s in lineup for s in slots):
                continue
            legs = [lineup[s] for s in slots]
            leg_probs = [leg_probability(s, era) for s in slots]
            if any(p is None for p in leg_probs):
                continue

            cons = trio_joint_prob(leg_probs, trio_type["pairs"], "conservative")
            opti = trio_joint_prob(leg_probs, trio_type["pairs"], "optimistic")

            candidates.append({
                "game_pk": game_pk,
                "team": team,
                "opp_team": lineup[1].get("opp_team"),
                "venue": lineup[1].get("venue"),
                "opp_era": era,
                "trio_type": trio_type["label"],
                "legs": [{
                    "player_id": leg.get("player_id"),
                    "name": leg.get("name"),
                    "order": leg.get("order"),
                    "leg_prob": round(p, 4),
                    "game_time_pt": leg.get("game_time_pt"),
                    "game_date_utc": leg.get("game_date_utc"),
                } for leg, p in zip(legs, leg_probs)],
                "conservative_prob": round(cons, 4),
                "optimistic_prob": round(opti, 4),
            })

    candidates.sort(key=lambda c: c["conservative_prob"], reverse=True)
    return candidates


def build_pairings(rows, max_pairings=3):
    """Pick up to max_pairings non-overlapping (different games, distinct
    trios) 2-trio pairings, ranked by combined conservative joint
    probability, restricted to pairings whose conservative combined
    probability clears PLAYABLE_MIN_CONSERVATIVE (>= 4.5%)."""
    trios = find_trio_candidates(rows)

    all_pairs = []
    for i in range(len(trios)):
        for j in range(i + 1, len(trios)):
            a, b = trios[i], trios[j]
            if a["game_pk"] == b["game_pk"]:
                continue
            cons = a["conservative_prob"] * b["conservative_prob"]
            opti = a["optimistic_prob"] * b["optimistic_prob"]
            all_pairs.append({
                "trios": [a, b],
                "combined_conservative": round(cons, 5),
                "combined_optimistic": round(opti, 5),
                "ev_conservative": round(PAYOUT * cons - 1, 3),
                "ev_optimistic": round(PAYOUT * opti - 1, 3),
                "playable": cons >= PLAYABLE_MIN_CONSERVATIVE,
            })

    all_pairs.sort(key=lambda p: p["combined_conservative"], reverse=True)

    selected = []
    used_trio_keys = set()
    for pairing in all_pairs:
        if not pairing["playable"]:
            continue
        keys = [(t["game_pk"], t["team"], t["trio_type"]) for t in pairing["trios"]]
        if any(k in used_trio_keys for k in keys):
            continue
        selected.append(pairing)
        used_trio_keys.update(keys)
        if len(selected) >= max_pairings:
            break

    return selected


# ---------------------------------------------------------------------------
# Persistence + self-updating rates refresh
# ---------------------------------------------------------------------------

STACKS_DIR = os.path.join("data", "stacks")


def save_stacks(date_str, pairings):
    os.makedirs(STACKS_DIR, exist_ok=True)
    out_path = os.path.join(STACKS_DIR, f"{date_str}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "pairings": pairings}, f, indent=2)
    return out_path


def _hrrbi_stats(results_files_data):
    """results_files_data: list of (date, raw_players_list, results_players_list)
    tuples, one per graded date. Computes fresh cross-table + adjacency
    stats from every player-game across all of them."""
    from collections import defaultdict

    order_era_counts = defaultdict(lambda: [0, 0])   # "ob|eb" -> [hits, total]
    order_counts = defaultdict(lambda: [0, 0])        # ob -> [hits, total]
    spot_counts = defaultdict(lambda: [0, 0])          # slot -> [hits, total]
    adjacency_counts = {  # "a,b" -> {hit_a: [hits_b, total_b], miss_a: [hits_b, total_b]}
        "1,2": [[0, 0], [0, 0]], "2,3": [[0, 0], [0, 0]], "3,4": [[0, 0], [0, 0]],
    }

    for date, raw_players, results_players in results_files_data:
        raw_by_pid = {r["player_id"]: r for r in raw_players}
        by_game_team = defaultdict(dict)
        hit2plus_by_pid = {}

        for p in results_players:
            al = p.get("actual_line") or {}
            hits = (al.get("singles") or 0) + (al.get("doubles") or 0) + (al.get("triples") or 0) + (al.get("hr") or 0)
            combined = hits + (al.get("runs") or 0) + (al.get("rbi") or 0)
            hit2plus_by_pid[p["player_id"]] = combined >= 2

            r = raw_by_pid.get(p["player_id"])
            if r is None or not r.get("batting_order"):
                continue
            order = r["batting_order"]
            if not (1 <= order <= 9):
                continue
            by_game_team[(r["game_pk"], r["team_id"])][order] = p["player_id"]

            ob = ORDER_BUCKET_OF_SLOT[order]
            mt = r.get("matchup") or {}
            era = mt.get("era") if mt.get("era") is not None else mt.get("era_fg")
            eb = era_bucket(era)
            hit = hit2plus_by_pid[p["player_id"]]

            order_counts[ob][1] += 1
            spot_counts[order][1] += 1
            if hit:
                order_counts[ob][0] += 1
                spot_counts[order][0] += 1
            if eb is not None:
                key = f"{ob}|{eb}"
                order_era_counts[key][1] += 1
                if hit:
                    order_era_counts[key][0] += 1

        for lineup in by_game_team.values():
            for a, b in ((1, 2), (2, 3), (3, 4)):
                pid_a, pid_b = lineup.get(a), lineup.get(b)
                if pid_a is None or pid_b is None:
                    continue
                a_hit = hit2plus_by_pid.get(pid_a)
                b_hit = hit2plus_by_pid.get(pid_b)
                if a_hit is None or b_hit is None:
                    continue
                bucket = adjacency_counts[f"{a},{b}"][0 if a_hit else 1]
                bucket[1] += 1
                if b_hit:
                    bucket[0] += 1

    cross_table = {k: [round(v[0] / v[1], 4), v[1]] for k, v in order_era_counts.items() if v[1] > 0}
    marginal = {k: round(v[0] / v[1], 4) for k, v in order_counts.items() if v[1] > 0}
    adjacency = {}
    for pair, (hit_bucket, miss_bucket) in adjacency_counts.items():
        n_total = hit_bucket[1] + miss_bucket[1]
        baseline_hits = hit_bucket[0] + miss_bucket[0]
        baseline = baseline_hits / n_total if n_total else None
        raw = hit_bucket[0] / hit_bucket[1] if hit_bucket[1] else None
        if baseline is None or raw is None:
            continue
        adjacency[pair] = {"baseline": round(baseline, 4), "raw": round(raw, 4), "n": hit_bucket[1]}

    return cross_table, marginal, adjacency


def refresh_hrrbi_rates(force=False):
    """Recompute the order x ERA cross-table and adjacency correlation
    stats from every data/results/results_*.json snapshot, at most once
    every RATES_REFRESH_DAYS days unless force=True. Called nightly by
    tracker.py. Cells below MIN_CELL_N graded plays keep the hand-computed
    default at load time (see _load_rates)."""
    if not force and os.path.exists(RATES_PATH):
        try:
            with open(RATES_PATH, encoding="utf-8") as f:
                existing = json.load(f)
            last = existing.get("computed_at")
            if last:
                age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).days
                if age_days < RATES_REFRESH_DAYS:
                    return existing
        except Exception:
            pass

    import glob
    results_files_data = []
    for rpath in sorted(glob.glob(os.path.join("data", "results", "results_*.json"))):
        date = os.path.basename(rpath).replace("results_", "").replace(".json", "")
        raw_path = os.path.join("data", f"{date}.json")
        if not os.path.exists(raw_path):
            continue
        with open(rpath, encoding="utf-8") as f:
            rdata = json.load(f)
        with open(raw_path, encoding="utf-8") as f:
            raw_players = json.load(f)
        results_files_data.append((date, raw_players, rdata.get("players", [])))

    cross_table, marginal, adjacency = _hrrbi_stats(results_files_data)
    payload = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "cross_table": cross_table,
        "marginal_order_rates": marginal,
        "adjacency": adjacency,
        "dates_used": len(results_files_data),
    }
    os.makedirs(os.path.dirname(RATES_PATH), exist_ok=True)
    with open(RATES_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return payload
