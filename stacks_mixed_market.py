"""
Mixed-market trio construction (Betr) — RESEARCH MODULE, NOT WIRED IN.

report.py still calls stacks.build_pairings(rows) (H+R+RBI-only, UD/PP-
anchored rows) for the live Stacks tab. This module is a separate,
reviewed-but-not-deployed capability: for each pair of consecutive
lineup slots in a trio, it picks whichever stat (RUNS, RBI, H+R+RBI, or
FANTASY_POINTS - whatever Betr actually posts for both players that day)
maximizes the deconfounded, correlation-adjusted joint probability,
instead of assuming H+R+RBI is the only option.

Built 2026-07-22 from the 32-date historical results dataset (order x
era leg calibration + adjacency) plus Betr's real market inventory
(scrapers/betr.py). Key findings that shaped this design:

- Betr's H+R+RBI market (key HITS_RUNS_RUNS_BATTED_IN, mostly the 1.5
  threshold) is by far its most reliable hitter market: 100-157
  players/day observed. RUNS (always 0.5) is solid and consistent
  (84-101/day). RUNS_BATTED_IN (always 0.5) is real but thin (16-18/day,
  ~11-18% of H+R+RBI's volume). FANTASY_POINTS is thin AND inconsistent
  (35 legs one day, 0 the next) - Betr's formula is IDENTICAL to
  PrizePicks' (1B+3 2B+5 3B+8 HR+10 BB/HBP+2 RBI/R+2 SB+5, confirmed via
  Betr's help center), so actual_pp/projected_pp double as its
  calibration data with no new formula needed, but its unreliable
  posting cadence means it should be treated as an occasional fallback,
  not a primary leg source.

- Full pairwise deconfounded-lift matrix (pt), A(slot N) -> B(slot N+1),
  pooled across (1,2)/(2,3)/(3,4)/(4,5)/(5,6):

           RUNS   RBI  HRRBI  FPTS
  RUNS     +6.7  +9.0   +7.4  +7.6
  RBI      +0.0  +3.4   +0.5  +1.7
  HRRBI    +1.3  +4.9   +2.1  +2.4
  FPTS     +0.7  +5.0   +1.5  +2.5

  RUNS->RBI (+9.0pt) is the single strongest cell in the matrix - the
  "direct chain" hypothesis was correct. The reverse, RBI->RUNS, shows
  essentially no adjacency-specific effect (+0.0pt): a hitter driving in
  a run ahead of them doesn't predict the next hitter scoring their own
  run the same way a hitter scoring (often via being driven in BY the
  next hitter, the same play) predicts that next hitter got the RBI.
  RUNS is also the strongest "leading" stat against everything it
  precedes - matches the mechanism (a runner on/scoring sets up the next
  at-bat's RBI/FPTS opportunity more than an isolated RBI does).

- H+R+RBI has both the highest standalone base rate AND is competitive
  on correlation, so in an idealized full-availability test it wins the
  stat-selection almost every time - mixed markets rarely beat H+R+RBI
  when H+R+RBI is actually posted for both legs. The real value of this
  module is covering the ~40-45% of players/day Betr does NOT post
  H+R+RBI for for, using RUNS/RBI/FPTS as a fallback instead of dropping
  that player from consideration entirely.

- Volume-gain caveat: we have NO historical Betr line postings (the
  scraper is new), so there is no true historical backtest possible.
  Two numbers instead: (a) an idealized ceiling assuming full RUNS+RBI+
  H+R+RBI availability for every player across all 32 historical dates -
  175 qualifying pairings vs 71 for the existing H+R+RBI-only fine-bucket
  baseline, but this mostly measures "what if coverage were 100%", not
  genuine mixed-market lift, since H+R+RBI dominates the selection
  whenever available; (b) one real test against 2026-07-20's actual
  cached Betr lines + real lineup data - 11 trio candidates built, but
  the strongest (0.171 conservative_prob) doesn't clear the 0.212 needed
  to self-pair at the 4.5% bar, so 0 qualifying pairings that day. n=1
  real day is nowhere near enough to draw a volume conclusion either
  way - this needs live monitoring over real days once Betr's live feed
  is wired into a day-to-day cache, the same way premium_tier needed
  real out-of-sample tracking before being trusted.
"""
import json
import os
import re
import unicodedata
from collections import defaultdict

SOFT_ERA_MIN = 4.50
PLAYABLE_MIN_CONSERVATIVE = 0.045
SHADOW_DIR = os.path.join("data", "stacks_shadow")
ORDER_BUCKET_OF_SLOT = {1: "1-2", 2: "1-2", 3: "3-4", 4: "3-4",
                         5: "5-6", 6: "5-6", 7: "7-9", 8: "7-9", 9: "7-9"}
TRIO_SLOT_SETS = {"1-2-3": (1, 2, 3), "2-3-4": (2, 3, 4)}

STATS = ["RUNS", "RBI", "HRRBI", "FPTS"]

# Betr stat key -> our stat label, and the dominant (calibrated) threshold
# for each. Non-dominant thresholds (e.g. H+R+RBI posted at 0.5 or 2.5
# instead of 1.5) aren't calibrated yet and are skipped.
BETR_STAT_MAP = {
    "RUNS": "RUNS",
    "RUNS_BATTED_IN": "RBI",
    "HITS_RUNS_RUNS_BATTED_IN": "HRRBI",
    "FANTASY_POINTS": "FPTS",
}
BETR_DOMINANT_THRESHOLD = {
    "RUNS": 0.5, "RUNS_BATTED_IN": 0.5, "HITS_RUNS_RUNS_BATTED_IN": 1.5, "FANTASY_POINTS": 6.5,
}

# order-bucket x era-bucket cross tables, computed 2026-07-22 from the
# 32-date results dataset (2026-06-14 to 2026-07-19). RUNS>=1, RBI>=1,
# HRRBI (H+R+RBI>=2), FPTS>=6.5 (PP-formula actual_pp, shared with Betr).
CROSS_TABLE = {
    "RUNS":  {"1-2": {"<3.50": 0.430, "3.50-4.49": 0.452, "4.50-5.49": 0.483, "5.50+": 0.538},
              "3-4": {"<3.50": 0.332, "3.50-4.49": 0.384, "4.50-5.49": 0.446, "5.50+": 0.510},
              "5-6": {"<3.50": 0.331, "3.50-4.49": 0.342, "4.50-5.49": 0.417, "5.50+": 0.355},
              "7-9": {"<3.50": 0.274, "3.50-4.49": 0.320, "4.50-5.49": 0.372, "5.50+": 0.391}},
    "RBI":   {"1-2": {"<3.50": 0.288, "3.50-4.49": 0.319, "4.50-5.49": 0.345, "5.50+": 0.317},
              "3-4": {"<3.50": 0.312, "3.50-4.49": 0.315, "4.50-5.49": 0.415, "5.50+": 0.389},
              "5-6": {"<3.50": 0.276, "3.50-4.49": 0.284, "4.50-5.49": 0.290, "5.50+": 0.310},
              "7-9": {"<3.50": 0.205, "3.50-4.49": 0.219, "4.50-5.49": 0.274, "5.50+": 0.301}},
    "HRRBI": {"1-2": {"<3.50": 0.498, "3.50-4.49": 0.524, "4.50-5.49": 0.541, "5.50+": 0.596},
              "3-4": {"<3.50": 0.457, "3.50-4.49": 0.456, "4.50-5.49": 0.543, "5.50+": 0.567},
              "5-6": {"<3.50": 0.411, "3.50-4.49": 0.447, "4.50-5.49": 0.463, "5.50+": 0.419},
              "7-9": {"<3.50": 0.351, "3.50-4.49": 0.376, "4.50-5.49": 0.429, "5.50+": 0.453}},
    "FPTS":  {"1-2": {"<3.50": 0.475, "3.50-4.49": 0.502, "4.50-5.49": 0.541, "5.50+": 0.543},
              "3-4": {"<3.50": 0.381, "3.50-4.49": 0.428, "4.50-5.49": 0.478, "5.50+": 0.514},
              "5-6": {"<3.50": 0.362, "3.50-4.49": 0.391, "4.50-5.49": 0.410, "5.50+": 0.350},
              "7-9": {"<3.50": 0.314, "3.50-4.49": 0.329, "4.50-5.49": 0.384, "5.50+": 0.399}},
}
MARGINAL = {
    "RUNS":  {"1-2": 0.461, "3-4": 0.394, "5-6": 0.356, "7-9": 0.324},
    "RBI":   {"1-2": 0.314, "3-4": 0.339, "5-6": 0.285, "7-9": 0.236},
    "HRRBI": {"1-2": 0.528, "3-4": 0.488, "5-6": 0.434, "7-9": 0.387},
    "FPTS":  {"1-2": 0.504, "3-4": 0.432, "5-6": 0.379, "7-9": 0.345},
}
# Deconfounded pairwise lift (pt), A(slot N) -> B(slot N+1). See module
# docstring for the full matrix and the RUNS->RBI / RBI->RUNS asymmetry.
PAIRWISE_LIFT_PT = {
    ("RUNS", "RUNS"): 6.7, ("RUNS", "RBI"): 9.0, ("RUNS", "HRRBI"): 7.4, ("RUNS", "FPTS"): 7.6,
    ("RBI", "RUNS"): 0.0, ("RBI", "RBI"): 3.4, ("RBI", "HRRBI"): 0.5, ("RBI", "FPTS"): 1.7,
    ("HRRBI", "RUNS"): 1.3, ("HRRBI", "RBI"): 4.9, ("HRRBI", "HRRBI"): 2.1, ("HRRBI", "FPTS"): 2.4,
    ("FPTS", "RUNS"): 0.7, ("FPTS", "RBI"): 5.0, ("FPTS", "HRRBI"): 1.5, ("FPTS", "FPTS"): 2.5,
}


def _stat_threshold(stat):
    """The 'hit' condition per stat, matching the calibration in
    CROSS_TABLE/MARGINAL above (RUNS>=1, RBI>=1, H+R+RBI>=2, FPTS>=6.5)."""
    return {"RUNS": 1, "RBI": 1, "HRRBI": 2, "FPTS": 6.5}[stat]


def stat_hit(stat, actual_line, actual_pp):
    """Grades one leg's actual outcome for the given stat."""
    if stat == "RUNS":
        return (actual_line.get("runs") or 0) >= 1
    if stat == "RBI":
        return (actual_line.get("rbi") or 0) >= 1
    if stat == "HRRBI":
        hits = ((actual_line.get("singles") or 0) + (actual_line.get("doubles") or 0)
                + (actual_line.get("triples") or 0) + (actual_line.get("hr") or 0))
        combined = hits + (actual_line.get("runs") or 0) + (actual_line.get("rbi") or 0)
        return combined >= 2
    if stat == "FPTS":
        return actual_pp is not None and actual_pp >= 6.5
    raise ValueError(f"unknown stat: {stat}")


def _era_bucket(era):
    if era is None:
        return None
    if era < 3.5:
        return "<3.50"
    if era < 4.5:
        return "3.50-4.49"
    if era < 5.5:
        return "4.50-5.49"
    return "5.50+"


def _normalize_name(name):
    name = unicodedata.normalize("NFKD", name or "")
    name = name.encode("ascii", "ignore").decode("ascii").lower()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def leg_probability(stat, slot, era):
    ob = ORDER_BUCKET_OF_SLOT[slot]
    eb = _era_bucket(era)
    if eb is not None:
        cell = CROSS_TABLE.get(stat, {}).get(ob, {}).get(eb)
        if cell is not None:
            return cell
    return MARGINAL[stat][ob]


def _adjacency_ratio(stat_a, stat_b, baseline_b):
    lift_pt = PAIRWISE_LIFT_PT[(stat_a, stat_b)]
    if baseline_b <= 0:
        return 1.0
    deconf_rate = baseline_b + max(0.0, lift_pt / 100)
    return max(1.0, deconf_rate / baseline_b)


def build_availability(raw_players, betr_lines):
    """raw_players: today's data/<date>.json list. betr_lines: output of
    scrapers.betr.get_betr_lines(date_str). Returns {player_id: set of
    available stats (RUNS/RBI/HRRBI/FPTS) at the calibrated dominant
    threshold}."""
    name_to_pid = {_normalize_name(r["name"]): r["player_id"] for r in raw_players}
    availability = defaultdict(set)
    for betr_key, our_stat in BETR_STAT_MAP.items():
        threshold = BETR_DOMINANT_THRESHOLD[betr_key]
        for name_key, line_value in betr_lines.get(betr_key, {}).items():
            if line_value != threshold:
                continue
            pid = name_to_pid.get(name_key)
            if pid is not None:
                availability[pid].add(our_stat)
    return availability


def _best_pair_stat_combo(slot_a, slot_b, era, avail_a, avail_b):
    best = None
    for stat_a in avail_a:
        p_a = leg_probability(stat_a, slot_a, era)
        for stat_b in avail_b:
            p_b = leg_probability(stat_b, slot_b, era)
            ratio = _adjacency_ratio(stat_a, stat_b, p_b)
            p_b_cond = min(1.0, p_b * ratio)
            joint = p_a * p_b_cond
            if best is None or joint > best[2]:
                best = (stat_a, stat_b, joint)
    return best


def _build_rows_by_team_game(raw_players):
    by_team_game = defaultdict(dict)
    for r in raw_players:
        order = r.get("batting_order")
        if not order or not (1 <= order <= 9):
            continue
        mt = r.get("matchup") or {}
        era = mt.get("era") if mt.get("era") is not None else mt.get("era_fg")
        key = (r.get("game_pk"), r.get("team_id"))
        by_team_game[key][order] = {"player_id": r.get("player_id"), "opp_era": era}
    return by_team_game


def find_mixed_market_trio_candidates(raw_players, availability):
    """raw_players: today's data/<date>.json list (for batting order/team/
    era/game_pk). availability: from build_availability(). Returns trio
    candidates picking, per adjacent pair, whichever available stat combo
    maximizes the deconfounded joint probability."""
    rows_by_team_game = _build_rows_by_team_game(raw_players)
    pid_to_name = {r.get("player_id"): r.get("name", "") for r in raw_players}
    candidates = []
    for (game_pk, team), lineup in rows_by_team_game.items():
        if 1 not in lineup:
            continue
        era = lineup[1]["opp_era"]
        if era is None or era < SOFT_ERA_MIN:
            continue
        for label, slots in TRIO_SLOT_SETS.items():
            if not all(s in lineup for s in slots):
                continue
            pid1, pid2, pid3 = (lineup[s]["player_id"] for s in slots)
            avail1, avail2, avail3 = availability.get(pid1, set()), availability.get(pid2, set()), availability.get(pid3, set())
            if not avail1 or not avail2 or not avail3:
                continue
            best_leg1_stat = max(avail1, key=lambda s: leg_probability(s, slots[0], era))
            p1 = leg_probability(best_leg1_stat, slots[0], era)
            combo12 = _best_pair_stat_combo(slots[0], slots[1], era, {best_leg1_stat}, avail2)
            combo23 = _best_pair_stat_combo(slots[1], slots[2], era, avail2, avail3)
            if combo12 is None or combo23 is None:
                continue
            _, stat2a, _ = combo12
            stat2b, stat3, _ = combo23
            if stat2a != stat2b:
                p2 = leg_probability(stat2a, slots[1], era)
                p3 = leg_probability(stat3, slots[2], era)
                joint = p1 * p2 * p3  # no adjacency credit when leg 2's stat can't serve both pairs
            else:
                p3_cond = min(1.0, leg_probability(stat3, slots[2], era)
                               * _adjacency_ratio(stat2b, stat3, leg_probability(stat3, slots[2], era)))
                p2_cond = min(1.0, leg_probability(stat2a, slots[1], era)
                               * _adjacency_ratio(best_leg1_stat, stat2a, leg_probability(stat2a, slots[1], era)))
                joint = p1 * p2_cond * p3_cond
            stats_used = (best_leg1_stat, stat2a, stat3)
            pids = (pid1, pid2, pid3)
            legs = [
                {"player_id": pid, "name": pid_to_name.get(pid, ""), "slot": slot,
                 "stat": stat, "threshold": _stat_threshold(stat)}
                for pid, slot, stat in zip(pids, slots, stats_used)
            ]
            candidates.append({
                "game_pk": game_pk, "team": team, "trio_type": label,
                "conservative_prob": round(joint, 4),
                "stats_used": stats_used,
                "legs": legs,
            })
    candidates.sort(key=lambda c: c["conservative_prob"], reverse=True)
    return candidates


def build_pairings(trios, max_pairings=3):
    """Same greedy non-overlapping-game selection as stacks.build_pairings,
    reimplemented here so this module has no import dependency on the live
    stacks.py (keeps the two paths fully independent, as intended for
    shadow mode)."""
    all_pairs = []
    for i in range(len(trios)):
        for j in range(i + 1, len(trios)):
            a, b = trios[i], trios[j]
            if a["game_pk"] == b["game_pk"]:
                continue
            cons = a["conservative_prob"] * b["conservative_prob"]
            if cons >= PLAYABLE_MIN_CONSERVATIVE:
                all_pairs.append((cons, a, b))
    all_pairs.sort(key=lambda p: p[0], reverse=True)

    selected = []
    used_keys = set()
    for cons, a, b in all_pairs:
        keys = [(a["game_pk"], a["team"], a["trio_type"]), (b["game_pk"], b["team"], b["trio_type"])]
        if any(k in used_keys for k in keys):
            continue
        selected.append({"trios": [a, b], "combined_conservative": round(cons, 5)})
        used_keys.update(keys)
        if len(selected) >= max_pairings:
            break
    return selected


# ---------------------------------------------------------------------------
# Narrow gap-filling test: does a RUNS fallback rescue trios that would be
# dropped when a player's H+R+RBI line isn't posted? This is the ONE piece
# the analysis says is this module's actual value (H+R+RBI wins the
# selection whenever it's available - see module docstring) - isolating
# it from the general "pick whatever correlates best" mixed-market logic
# so its rescue rate can be measured directly, in shadow mode only.
# ---------------------------------------------------------------------------

def build_hrrbi_gated_availability(raw_players, betr_lines, allow_runs_fallback):
    """HRRBI-only availability, gated on a REAL posted Betr H+R+RBI line
    (unlike live stacks.py, which isn't gated on any market's real
    presence at all). If allow_runs_fallback, players missing an HRRBI
    line but WITH a posted RUNS line get {"RUNS"} instead of an empty set,
    so the trio isn't dropped outright."""
    name_to_pid = {_normalize_name(r["name"]): r["player_id"] for r in raw_players}
    hrrbi_lines = betr_lines.get("HITS_RUNS_RUNS_BATTED_IN", {})
    runs_lines = betr_lines.get("RUNS", {})

    availability = defaultdict(set)
    for name_key, threshold in hrrbi_lines.items():
        if threshold != BETR_DOMINANT_THRESHOLD["HITS_RUNS_RUNS_BATTED_IN"]:
            continue
        pid = name_to_pid.get(name_key)
        if pid is not None:
            availability[pid].add("HRRBI")

    if allow_runs_fallback:
        for name_key, threshold in runs_lines.items():
            if threshold != BETR_DOMINANT_THRESHOLD["RUNS"]:
                continue
            pid = name_to_pid.get(name_key)
            if pid is not None and "HRRBI" not in availability[pid]:
                availability[pid].add("RUNS")

    return availability


def run_gap_filling_shadow(date_str, raw_players, betr_lines):
    """Compares HRRBI-only-gated-on-real-Betr-lines (baseline) against
    HRRBI-with-RUNS-fallback (only substituting RUNS where HRRBI is
    missing for that specific player), reporting the pairing-count delta
    and exactly which trios were only possible because of the fallback."""
    baseline_avail = build_hrrbi_gated_availability(raw_players, betr_lines, allow_runs_fallback=False)
    fallback_avail = build_hrrbi_gated_availability(raw_players, betr_lines, allow_runs_fallback=True)

    baseline_trios = find_mixed_market_trio_candidates(raw_players, baseline_avail)
    fallback_trios = find_mixed_market_trio_candidates(raw_players, fallback_avail)

    baseline_pairings = build_pairings(baseline_trios)
    fallback_pairings = build_pairings(fallback_trios)

    baseline_keys = {(t["game_pk"], t["team"], t["trio_type"]) for t in baseline_trios}
    rescued_trios = [t for t in fallback_trios
                     if "RUNS" in t["stats_used"] and (t["game_pk"], t["team"], t["trio_type"]) not in baseline_keys]

    return {
        "date": date_str,
        "baseline_trio_count": len(baseline_trios),
        "fallback_trio_count": len(fallback_trios),
        "baseline_pairing_count": len(baseline_pairings),
        "fallback_pairing_count": len(fallback_pairings),
        "rescued_trio_count": len(rescued_trios),
        "rescued_trios": rescued_trios,
        "fallback_pairings": fallback_pairings,
    }


def run_shadow_mode(date_str, raw_players, betr_lines):
    """Computes everything shadow-mode needs for one day: the general
    mixed-market pairings (all 4 stats, gated on real Betr postings) AND
    the narrow HRRBI+RUNS-fallback gap-filling comparison. No dashboard
    output, no bets - meant to be saved to data/stacks_shadow/<date>.json
    and graded nightly (see tracker.run_stacks_shadow_grading) against a
    running record kept fully separate from the live stacks results."""
    availability = build_availability(raw_players, betr_lines)
    mixed_trios = find_mixed_market_trio_candidates(raw_players, availability)
    mixed_pairings = build_pairings(mixed_trios)
    gap_filling = run_gap_filling_shadow(date_str, raw_players, betr_lines)

    return {
        "date": date_str,
        "mixed_market": {
            "trio_count": len(mixed_trios),
            "pairings": mixed_pairings,
        },
        "gap_filling": gap_filling,
    }


def save_shadow(date_str, shadow_data):
    os.makedirs(SHADOW_DIR, exist_ok=True)
    out_path = os.path.join(SHADOW_DIR, f"{date_str}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(shadow_data, f, indent=2)
    return out_path


def load_shadow(date_str):
    path = os.path.join(SHADOW_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
