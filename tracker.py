"""
MLB Fantasy Results Tracker
Compares each player's projected UD/PP fantasy points (as shown on the
dashboard) against their actual box-score performance for a given date.
Also tracks hit/miss for the daily Top 25 specifically.

Usage:
  python tracker.py              # grades yesterday's games
  python tracker.py 2026-06-10   # grades a specific date
"""

import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import projections as proj
import report
from scrapers.market_lines import compute_pp_ud_ratio, load_cached_market_lines, match_lines
from scrapers.mlb_api import get_player_game_log

HIT_TOLERANCE = 0.20  # within 20% of projection counts as a "hit"

import slips as slips_mod
import stacks as stacks_mod
import stacks_mixed_market as stacks_mixed_mod

RESULTS_DIR = os.path.join("data", "results")
ALL_RESULTS_PATH = os.path.join(RESULTS_DIR, "all_results.json")
TOP25_RESULTS_PATH = os.path.join(RESULTS_DIR, "top25_results.json")
VALUE_PLAYS_DIR = os.path.join("data", "value_plays")
VALUE_PLAYS_RESULTS_PATH = os.path.join(RESULTS_DIR, "value_plays_results.json")
SLIPS_RESULTS_PATH = os.path.join(RESULTS_DIR, "slips_results.json")
PREMIUM_RESULTS_PATH = os.path.join(RESULTS_DIR, "premium_results.json")
STACKS_RESULTS_PATH = os.path.join(RESULTS_DIR, "stacks_results.json")
STACKS_SHADOW_RESULTS_PATH = os.path.join(RESULTS_DIR, "stacks_shadow_results.json")
UD_UNDER_BAND_RESULTS_PATH = os.path.join(RESULTS_DIR, "ud_under_band_results.json")
MARKED_PLAYS_RESULTS_PATH = os.path.join(RESULTS_DIR, "marked_plays_results.json")

# Fire/Hot marked-play cohort thresholds - must match report.top25TreatmentClass's
# live badge logic (n>=8 minimum sample, 55%+ bar, n>=15 for the "fire" glow
# vs. n 8-14 for "hot"). Kept as separate constants here (not imported from
# report.py) because grading needs to reconstruct each date's classification
# point-in-time, not read report.py's current-running-total badge.
FIRE_MIN_N = 15
HOT_MIN_N = 8
MARK_MIN_RATE = 0.55


def classify(projected, actual):
    if projected <= 0:
        return "push" if actual <= 0 else "over"
    diff = actual - projected
    if abs(diff) <= HIT_TOLERANCE * projected:
        return "hit"
    return "over" if diff > 0 else "under"


def call_direction(projected, line):
    """OVER/UNDER call implied by our projection relative to the posted
    UD/PP line. None if there's no line to call against.

    When projected == line (exact tie), the call is OVER — the projection
    agrees with the market's number and the natural side is that the player
    reaches it. The only true push is when actual == line (graded in
    grade_vs_line), not when our projection happens to match the line."""
    if line is None:
        return None
    if projected >= line:
        return "over"
    return "under"


def grade_vs_line(call, actual, line):
    """Win/loss for a directional call against the posted line: a hit means
    our OVER/UNDER call matched which side of the line the actual result
    landed on, regardless of how close our exact point projection was.
    A push (call or outcome landed exactly on the line) is excluded from
    win/loss - there was no real over/under to be right or wrong about."""
    if call is None or line is None:
        return None
    if actual > line:
        outcome = "over"
    elif actual < line:
        outcome = "under"
    else:
        outcome = "push"
    if call == "push" or outcome == "push":
        return "push"
    return "win" if call == outcome else "loss"


def summarize_vs_line(players):
    """Directional hit rate vs the UD/PP line, across every graded player
    (not just Value Plays) - this is what's shown as the dashboard's overall
    hit rate."""
    summary = {}
    for key in ("ud", "pp"):
        win = sum(1 for p in players if p[f"result_{key}"] == "win")
        loss = sum(1 for p in players if p[f"result_{key}"] == "loss")
        push = sum(1 for p in players if p[f"result_{key}"] == "push")
        total = win + loss
        summary[key] = {
            "total": total,
            "win": win,
            "loss": loss,
            "push": push,
            "hit_rate": round(100 * win / total, 1) if total else 0.0,
        }
    return summary



def summarize(players):
    summary = {}
    for key in ("ud", "pp"):
        hit = sum(1 for p in players if p[f"result_{key}"] == "hit")
        over = sum(1 for p in players if p[f"result_{key}"] == "over")
        under = sum(1 for p in players if p[f"result_{key}"] == "under")
        total = hit + over + under
        summary[key] = {
            "total": total,
            "hit": hit,
            "over": over,
            "under": under,
            "hit_rate": round(100 * hit / total, 1) if total else 0.0,
        }
    return summary


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def track_date(date_str):
    data_path = os.path.join("data", f"{date_str}.json")
    if not os.path.exists(data_path):
        print(f"No projection data for {date_str} ({data_path} not found). Skipping.")
        return None

    with open(data_path, encoding="utf-8") as f:
        raw_players = json.load(f)

    # Reconstruct the projections as they would have appeared on that day's
    # dashboard: recalibrated, then adjusted by whatever per-player
    # correction factors were in effect at the time (i.e. before today's
    # update to all_results.json).
    existing_all = load_json(ALL_RESULTS_PATH, {"dates": {}, "players": {}})
    corrections = report.build_corrections(existing_all)

    rows = [report.build_row(p) for p in raw_players]
    report.recalibrate_points(rows)
    report.apply_venue_penalty(rows)

    # Anchor to the day's posted UD/PP lines *before* grading - this is the
    # same projection the live dashboard showed that day. Grading the
    # unanchored percentile-curve value (as before) compared the wrong
    # number against actual results.
    market_lines = load_cached_market_lines(date_str)
    market_corrections = existing_all.get("market_corrections", {})
    anchored_ids = report.apply_market_anchor(rows, market_lines, market_corrections)
    report.apply_corrections(rows, corrections, skip=anchored_ids)
    report.apply_no_line_penalty(rows, anchored_ids)

    results = []
    for row in rows:
        player_id = row["player_id"]
        if not player_id:
            continue

        try:
            line = get_player_game_log(player_id, date_str)
        except Exception as e:
            print(f"  warning: game log failed for {row['name']}: {e}")
            line = None

        if not line or not line.get("games"):
            continue  # didn't play (scratched, postponed, bench, etc.)

        actual_ud = proj._score(line, proj.UD_SCORING)
        actual_pp = proj._score(line, proj.PP_SCORING)
        proj_ud = row["ud_pts"]
        proj_pp = row["pp_pts"]

        ud_line = row.get("ud_line")
        pp_line = row.get("pp_line")
        ud_call = call_direction(proj_ud, ud_line)
        pp_call = call_direction(proj_pp, pp_line)

        results.append({
            "player_id": player_id,
            "name": row["name"],
            "team": row["team"],
            "projected_ud": proj_ud,
            "ud_line": ud_line,
            "actual_ud": actual_ud,
            "result_ud": grade_vs_line(ud_call, actual_ud, ud_line),
            "projected_pp": proj_pp,
            "pp_line": pp_line,
            "actual_pp": actual_pp,
            "result_pp": grade_vs_line(pp_call, actual_pp, pp_line),
            "actual_line": {k: line[k] for k in
                            ("singles", "doubles", "triples", "hr", "bb", "hbp", "rbi", "runs", "sb")},
            "game_time_pt": row.get("game_time_pt"),
        })

    summary = summarize_vs_line(results)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_path = os.path.join(RESULTS_DIR, f"results_{date_str}.json")
    payload = {"date": date_str, "summary": summary, "players": results}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Saved {len(results)} results -> {out_path}")
    print(f"UD: {summary['ud']['hit_rate']}% directional hit rate "
          f"({summary['ud']['win']} win / {summary['ud']['loss']} loss / {summary['ud']['push']} push)")
    print(f"PP: {summary['pp']['hit_rate']}% directional hit rate "
          f"({summary['pp']['win']} win / {summary['pp']['loss']} loss / {summary['pp']['push']} push)")

    update_all_results(date_str, summary, results)

    results_by_pid = {r["player_id"]: r for r in results}
    track_top25(date_str, rows, results_by_pid)
    grade_value_plays(date_str, results_by_pid)
    grade_ud_under_band(date_str, results_by_pid)
    # Premium/Slips/Stacks (and Stacks shadow-mode) retired from the live
    # dashboard 2026-08-18 - see report.py/slips.py/stacks.py. No point
    # accumulating win/loss records for products nobody sees anymore, so
    # nightly grading for all four is off. The functions themselves (and
    # their historical data/results/*.json records) are kept as-is; unwire,
    # don't delete - matches the PP retirement pattern. Re-enable by calling
    # them here again if any of these ever comes back.
    # grade_premium_plays(date_str, results_by_pid)
    # grade_slips(date_str, results_by_pid)
    # grade_stacks(date_str, results)
    # grade_stacks_shadow(date_str, results)

    # Weekly refresh of the edge-bucket win-rate table slips.py scores legs
    # with (see EDGE_BUCKET_REFRESH_DAYS) — keeps it from ever going stale
    # again without a manual update.
    try:
        slips_mod.refresh_edge_bucket_rates(load_json(ALL_RESULTS_PATH, {"dates": {}, "players": {}}))
    except Exception as e:
        print(f"Edge-bucket rate refresh failed (non-fatal): {e}")
    try:
        slips_mod.refresh_pp_edge_bucket_rates(load_json(ALL_RESULTS_PATH, {"dates": {}, "players": {}}))
    except Exception as e:
        print(f"PP edge-bucket rate refresh failed (non-fatal): {e}")

    # Weekly refresh of the stack optimizer's order x ERA cross-table and
    # adjacency correlation stats (see stacks.RATES_REFRESH_DAYS).
    try:
        stacks_mod.refresh_hrrbi_rates()
    except Exception as e:
        print(f"H+R+RBI rate refresh failed (non-fatal): {e}")

    # Regenerate the dashboard with the freshly-updated Results / Player
    # History / Value Plays data and push it to GitHub Pages, so the site
    # picks up overnight grading without waiting for the next pipeline run.
    try:
        report.regenerate_dashboard()
    except Exception as e:
        print(f"Dashboard regeneration/deploy failed (non-fatal): {e}")

    return payload


def grade_value_plays(date_str, results_by_pid):
    """Grade each of the day's Value Plays (OVER/UNDER calls vs. the posted
    UD market line) against actual results.

    A call is correct if:
      - OVER  call: actual UD points > market line
      - UNDER call: actual UD points < market line
    """
    plays_path = os.path.join(VALUE_PLAYS_DIR, f"{date_str}.json")
    if not os.path.exists(plays_path):
        print(f"No Value Plays recorded for {date_str} ({plays_path} not found). Skipping.")
        return

    with open(plays_path, encoding="utf-8") as f:
        plays_data = json.load(f)

    market_lines = load_cached_market_lines(date_str)
    pp_ud_ratio = compute_pp_ud_ratio(market_lines) if market_lines else None

    graded = []
    for play in plays_data.get("plays", []):
        res = results_by_pid.get(play["player_id"])
        if not res:
            continue  # player didn't play (scratched, postponed, etc.)

        ud_line = play.get("ud_line")
        if market_lines:
            line, _ = match_lines(play["name"], market_lines, pp_ud_ratio)
            if line is not None:
                ud_line = line

        actual_ud = res["actual_ud"]
        if play["call"] == "over":
            correct = actual_ud > ud_line
        else:
            correct = actual_ud < ud_line

        graded.append({
            "player_id": play["player_id"],
            "name": play["name"],
            "team": play["team"],
            "call": play["call"],
            "edge": play["edge"],
            "ud_line": ud_line,
            "actual_ud": actual_ud,
            "correct": correct,
        })

    total = len(graded)
    hits = sum(1 for g in graded if g["correct"])
    over_graded = [g for g in graded if g["call"] == "over"]
    under_graded = [g for g in graded if g["call"] == "under"]
    summary = {
        "total": total,
        "correct": hits,
        "accuracy": round(100 * hits / total, 1) if total else 0.0,
        "over_total": len(over_graded),
        "over_correct": sum(1 for g in over_graded if g["correct"]),
        "under_total": len(under_graded),
        "under_correct": sum(1 for g in under_graded if g["correct"]),
    }

    vp_results = load_json(VALUE_PLAYS_RESULTS_PATH, {"dates": {}})
    vp_results.setdefault("dates", {})
    vp_results["dates"][date_str] = {"summary": summary, "plays": graded}

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(VALUE_PLAYS_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(vp_results, f, indent=2)

    print(f"Value Plays: {hits}/{total} correct ({summary['accuracy']}%) -> {VALUE_PLAYS_RESULTS_PATH}")


def grade_ud_under_band(date_str, results_by_pid):
    """Grade the day's UD UNDER 1.5-2.0-edge calls - the one finding from
    the 2026-08-18 OVER-vs-UNDER analysis that replicated in and out of
    sample (see report.UD_UNDER_BAND_* and the module comment there).

    Reads the day's candidate list from report.save_ud_under_band_plays'
    snapshot (data/ud_under_band_plays/{date}.json) rather than
    reconstructing candidacy from all_results.json - a DNP'd player never
    gets an all_results.json history entry at all (pipeline.py skips them
    outright), so scanning history after the fact can't tell "wasn't in
    the band" apart from "was in the band but didn't play." The snapshot
    is the only place that distinction survives.

    DNPs are counted separately and excluded from the win/loss record
    entirely - being right or wrong requires the game to have happened.
    Only dates after report.UD_UNDER_BAND_SEED_END count, so the frozen
    566-play seed (which predates DNP tracking) is never double-counted."""
    if date_str <= report.UD_UNDER_BAND_SEED_END:
        print(f"UD UNDER 1.5-2.0 band: {date_str} is in the seed window (through "
              f"{report.UD_UNDER_BAND_SEED_END}), not graded separately.")
        return

    plays_path = os.path.join(report.UD_UNDER_BAND_PLAYS_DIR, f"{date_str}.json")
    if not os.path.exists(plays_path):
        print(f"UD UNDER 1.5-2.0 band: no saved plays snapshot for {date_str} "
              f"(dashboard wasn't generated that day?) - skipping.")
        return
    candidates = load_json(plays_path, {"plays": []}).get("plays", [])

    wins = losses = dnp = 0
    graded_plays = []
    for play in candidates:
        res = results_by_pid.get(play["player_id"])
        if res is None:
            grade = "dnp"
            dnp += 1
            actual_ud = None
        else:
            actual_ud = res.get("actual_ud")
            result_ud = res.get("result_ud")
            if result_ud == "win":
                grade, wins = "win", wins + 1
            elif result_ud == "loss":
                grade, losses = "loss", losses + 1
            elif result_ud == "push":
                grade = "push"
            else:
                grade, dnp = "dnp", dnp + 1
        graded_plays.append({**play, "actual_ud": actual_ud, "grade": grade})

    data = load_json(UD_UNDER_BAND_RESULTS_PATH, {"seed": report.UD_UNDER_BAND_SEED, "dates": {}, "record": {"wins": 0, "losses": 0, "dnp": 0}})
    data.setdefault("dates", {})
    entry = {"wins": wins, "losses": losses, "dnp": dnp, "plays": graded_plays}
    if date_str in report.LOW_COVERAGE_DATES:
        entry["low_coverage"] = True
        entry["low_coverage_reason"] = report.LOW_COVERAGE_DATES[date_str]
    data["dates"][date_str] = entry

    # LOW_COVERAGE_DATES are excluded from the cumulative record - a
    # 15-33-line board can't produce a representative band sample (see
    # report.LOW_COVERAGE_DATES). The record is always fully recomputed
    # from data["dates"] rather than accumulated incrementally, so this
    # exclusion applies retroactively every time this runs, not just going
    # forward.
    counted = {d: dd for d, dd in data["dates"].items() if not dd.get("low_coverage")}
    data["record"] = {
        "wins": sum(d["wins"] for d in counted.values()),
        "losses": sum(d["losses"] for d in counted.values()),
        "dnp": sum(d.get("dnp", 0) for d in counted.values()),
    }

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(UD_UNDER_BAND_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    seed = report.UD_UNDER_BAND_SEED
    total_wins = seed["wins"] + data["record"]["wins"]
    total_losses = seed["losses"] + data["record"]["losses"]
    total_n = total_wins + total_losses
    rate = round(100 * total_wins / total_n, 1) if total_n else 0.0
    print(f"UD UNDER 1.5-2.0 band: {wins}-{losses} today ({dnp} DNP) -> live-tracked "
          f"{data['record']['wins']}-{data['record']['losses']} ({data['record']['dnp']} DNP), combined with seed "
          f"{total_wins}-{total_losses} ({rate}%, n={total_n})")


def grade_premium_plays(date_str, results_by_pid):
    """Grade each of the day's Premium (Tier A) / Strong (Tier B) calls
    against actual results and roll them into a running win/loss record
    per tier, tracked separately from Value Plays. This running record is
    the number that confirms (or fails to confirm) the 67.8% / 64.1%
    backtested rates hold out of sample — see slips.premium_tier."""
    plays_path = os.path.join(report.PREMIUM_PLAYS_DIR, f"{date_str}.json")
    if not os.path.exists(plays_path):
        print(f"No Premium plays recorded for {date_str} ({plays_path} not found). Skipping.")
        return

    with open(plays_path, encoding="utf-8") as f:
        plays_data = json.load(f)

    market_lines = load_cached_market_lines(date_str)
    pp_ud_ratio = compute_pp_ud_ratio(market_lines) if market_lines else None

    graded = []
    for play in plays_data.get("plays", []):
        res = results_by_pid.get(play["player_id"])
        if not res:
            continue

        ud_line = play.get("ud_line")
        if market_lines:
            line, _ = match_lines(play["name"], market_lines, pp_ud_ratio)
            if line is not None:
                ud_line = line

        actual_ud = res["actual_ud"]
        correct = (actual_ud > ud_line) if play["call"] == "over" else (actual_ud < ud_line)

        graded.append({
            "player_id": play["player_id"],
            "name": play["name"],
            "team": play["team"],
            "call": play["call"],
            "edge": play["edge"],
            "ud_line": ud_line,
            "actual_ud": actual_ud,
            "tier": play["tier"],
            "correct": correct,
        })

    all_pr = load_json(PREMIUM_RESULTS_PATH, {"dates": {}, "record": {}})
    all_pr.setdefault("dates", {})
    all_pr.setdefault("record", {})

    summary_by_tier = {}
    for tier in ("premium", "strong"):
        tier_graded = [g for g in graded if g["tier"] == tier]
        total = len(tier_graded)
        hits = sum(1 for g in tier_graded if g["correct"])
        summary_by_tier[tier] = {
            "total": total,
            "correct": hits,
            "win_rate": round(100 * hits / total, 1) if total else 0.0,
        }
        rec = all_pr["record"].setdefault(tier, {"wins": 0, "losses": 0})
        rec["wins"] += hits
        rec["losses"] += (total - hits)

    all_pr["dates"][date_str] = {"summary": summary_by_tier, "plays": graded}

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(PREMIUM_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(all_pr, f, indent=2)

    for tier in ("premium", "strong"):
        s = summary_by_tier[tier]
        rec = all_pr["record"][tier]
        rec_total = rec["wins"] + rec["losses"]
        rec_rate = round(100 * rec["wins"] / rec_total, 1) if rec_total else 0.0
        print(f"  {tier.capitalize()}: {s['correct']}/{s['total']} today ({s['win_rate']}%) "
              f"-> running record {rec['wins']}-{rec['losses']} ({rec_rate}%)")

    eligible, status = slips_mod.strong_tier_reinclusion_status(all_pr["record"]["strong"])
    print(f"  {'[ELIGIBLE]' if eligible else '[excluded]'} {status}")


def grade_stacks(date_str, results):
    """Grade each of the day's recommended stack pairings: per-leg
    H+R+RBI>=2 outcome, per-trio (all 3 legs hit), and the full 6-man
    (both trios hit). Rolls into a running record per level so actual
    out-of-sample performance can be compared against the predicted
    conservative-to-optimistic probability range."""
    plays_path = os.path.join(stacks_mod.STACKS_DIR, f"{date_str}.json")
    if not os.path.exists(plays_path):
        print(f"No stack pairings recorded for {date_str} ({plays_path} not found). Skipping.")
        return

    with open(plays_path, encoding="utf-8") as f:
        stacks_data = json.load(f)

    hit2plus_by_pid = {}
    for r in results:
        al = r.get("actual_line") or {}
        hits = (al.get("singles") or 0) + (al.get("doubles") or 0) + (al.get("triples") or 0) + (al.get("hr") or 0)
        hit2plus_by_pid[r["player_id"]] = (hits + (al.get("runs") or 0) + (al.get("rbi") or 0)) >= 2

    default_record = {"legs": {"wins": 0, "losses": 0}, "trios": {"wins": 0, "losses": 0}, "full6": {"wins": 0, "losses": 0}}
    all_sr = load_json(STACKS_RESULTS_PATH, {"dates": {}, "record": default_record})
    all_sr.setdefault("dates", {})
    all_sr.setdefault("record", default_record)
    for level in ("legs", "trios", "full6"):
        all_sr["record"].setdefault(level, {"wins": 0, "losses": 0})

    graded_pairings = []
    for pairing in stacks_data.get("pairings", []):
        graded_trios = []
        all_trios_hit = True
        for trio in pairing["trios"]:
            graded_legs = []
            trio_hit = True
            trio_fully_graded = True
            for leg in trio["legs"]:
                pid = leg["player_id"]
                if pid not in hit2plus_by_pid:
                    trio_fully_graded = False
                    graded_legs.append({**leg, "actual_hit": None})
                    continue
                hit = hit2plus_by_pid[pid]
                graded_legs.append({**leg, "actual_hit": hit})
                trio_hit = trio_hit and hit
                rec = all_sr["record"]["legs"]
                rec["wins" if hit else "losses"] += 1

            if trio_fully_graded:
                rec = all_sr["record"]["trios"]
                rec["wins" if trio_hit else "losses"] += 1
            else:
                trio_hit = False  # can't count as a hit if a leg didn't play
            all_trios_hit = all_trios_hit and trio_hit
            graded_trios.append({**trio, "legs": graded_legs, "trio_hit": trio_hit, "fully_graded": trio_fully_graded})

        pairing_fully_graded = all(t["fully_graded"] for t in graded_trios)
        if pairing_fully_graded:
            rec = all_sr["record"]["full6"]
            rec["wins" if all_trios_hit else "losses"] += 1

        graded_pairings.append({
            **{k: v for k, v in pairing.items() if k != "trios"},
            "trios": graded_trios,
            "full6_hit": all_trios_hit if pairing_fully_graded else False,
            "fully_graded": pairing_fully_graded,
        })

    all_sr["dates"][date_str] = {"pairings": graded_pairings}
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(STACKS_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(all_sr, f, indent=2)

    def _rate(r):
        t = r["wins"] + r["losses"]
        return round(100 * r["wins"] / t, 1) if t else 0.0

    rec = all_sr["record"]
    print(f"  Stacks: legs {rec['legs']['wins']}-{rec['legs']['losses']} ({_rate(rec['legs'])}%)  "
          f"trios {rec['trios']['wins']}-{rec['trios']['losses']} ({_rate(rec['trios'])}%)  "
          f"full6 {rec['full6']['wins']}-{rec['full6']['losses']} ({_rate(rec['full6'])}%)")


def grade_stacks_shadow(date_str, results):
    """Grades the day's shadow-mode mixed-market output (see
    stacks_mixed_market.run_shadow_mode, computed daily in report.py) -
    fully separate running record from grade_stacks' live one. Tracks the
    general mixed-market legs/trios/full6 record AND the narrow
    gap-filling stat specifically: how many times the RUNS fallback fired
    (rescued a trio that had no H+R+RBI-only candidate), and whether
    those rescued RUNS legs actually hit. No dashboard output, no bets -
    this is purely to accumulate a real record before any live decision."""
    shadow = stacks_mixed_mod.load_shadow(date_str)
    if shadow is None:
        print(f"No shadow-mode data for {date_str}. Skipping shadow grading.")
        return

    actual_by_pid = {r["player_id"]: (r.get("actual_line") or {}, r.get("actual_pp")) for r in results}

    def grade_leg(leg):
        if leg["player_id"] not in actual_by_pid:
            return None
        al, actual_pp = actual_by_pid[leg["player_id"]]
        return stacks_mixed_mod.stat_hit(leg["stat"], al, actual_pp)

    default_record = {"legs": {"wins": 0, "losses": 0}, "trios": {"wins": 0, "losses": 0}, "full6": {"wins": 0, "losses": 0}}
    default_gap = {"rescue_fire_count": 0, "days_tracked": 0, "rescued_trio_legs_graded": 0, "rescued_trio_legs_hit": 0}
    all_ssr = load_json(STACKS_SHADOW_RESULTS_PATH, {"dates": {}, "record": default_record, "gap_filling_record": default_gap})
    all_ssr.setdefault("dates", {})
    all_ssr.setdefault("record", default_record)
    all_ssr.setdefault("gap_filling_record", default_gap)
    for level in ("legs", "trios", "full6"):
        all_ssr["record"].setdefault(level, {"wins": 0, "losses": 0})

    graded_pairings = []
    for pairing in shadow.get("mixed_market", {}).get("pairings", []):
        graded_trios = []
        all_hit = True
        for trio in pairing["trios"]:
            graded_legs = []
            trio_hit = True
            fully_graded = True
            for leg in trio["legs"]:
                hit = grade_leg(leg)
                if hit is None:
                    fully_graded = False
                    graded_legs.append({**leg, "actual_hit": None})
                    continue
                graded_legs.append({**leg, "actual_hit": hit})
                trio_hit = trio_hit and hit
                rec = all_ssr["record"]["legs"]
                rec["wins" if hit else "losses"] += 1
            if fully_graded:
                rec = all_ssr["record"]["trios"]
                rec["wins" if trio_hit else "losses"] += 1
            else:
                trio_hit = False
            all_hit = all_hit and trio_hit
            graded_trios.append({**trio, "legs": graded_legs, "trio_hit": trio_hit, "fully_graded": fully_graded})

        pairing_fully_graded = all(t["fully_graded"] for t in graded_trios)
        if pairing_fully_graded:
            rec = all_ssr["record"]["full6"]
            rec["wins" if all_hit else "losses"] += 1
        graded_pairings.append({"trios": graded_trios, "full6_hit": all_hit if pairing_fully_graded else False,
                                 "fully_graded": pairing_fully_graded})

    gap = shadow.get("gap_filling", {})
    gap_rec = all_ssr["gap_filling_record"]
    gap_rec["rescue_fire_count"] += gap.get("rescued_trio_count", 0)
    gap_rec["days_tracked"] += 1
    graded_rescued = []
    for trio in gap.get("rescued_trios", []):
        graded_legs = []
        for leg in trio["legs"]:
            hit = grade_leg(leg)
            graded_legs.append({**leg, "actual_hit": hit})
            if leg["stat"] == "RUNS" and hit is not None:
                gap_rec["rescued_trio_legs_graded"] += 1
                if hit:
                    gap_rec["rescued_trio_legs_hit"] += 1
        graded_rescued.append({**trio, "legs": graded_legs})

    all_ssr["dates"][date_str] = {"mixed_market_pairings": graded_pairings, "gap_filling_rescued_trios": graded_rescued}
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(STACKS_SHADOW_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(all_ssr, f, indent=2)

    def _rate(r):
        t = r["wins"] + r["losses"]
        return round(100 * r["wins"] / t, 1) if t else 0.0

    rec = all_ssr["record"]
    print(f"  [SHADOW] Mixed-market: legs {rec['legs']['wins']}-{rec['legs']['losses']} ({_rate(rec['legs'])}%)  "
          f"trios {rec['trios']['wins']}-{rec['trios']['losses']} ({_rate(rec['trios'])}%)  "
          f"full6 {rec['full6']['wins']}-{rec['full6']['losses']} ({_rate(rec['full6'])}%)")
    print(f"  [SHADOW] Gap-filling: fired {gap_rec['rescue_fire_count']}x over {gap_rec['days_tracked']} day(s) tracked; "
          f"rescued-trio RUNS legs {gap_rec['rescued_trio_legs_hit']}/{gap_rec['rescued_trio_legs_graded']} hit")


def _grade_single_slip(slip, platform, results_by_pid):
    """Grade one slip dict, return graded dict with leg results."""
    graded_legs = []
    for leg in slip.get("legs", []):
        pid = leg["player_id"]
        res = results_by_pid.get(pid)
        if not res:
            continue
        actual = res["actual_pp"] if platform == "pp" else res["actual_ud"]
        if actual is None:
            continue
        line = leg["line"]
        call = leg["call"]
        if actual > line:
            result = "win" if call == "over" else "loss"
        elif actual < line:
            result = "win" if call == "under" else "loss"
        else:
            result = "push"
        graded_legs.append({**leg, "actual": round(actual, 2), "result": result})

    decided  = [l for l in graded_legs if l["result"] != "push"]
    legs_win = sum(1 for l in decided if l["result"] == "win")
    slip_win = bool(decided) and legs_win == len(slip["legs"])
    return {
        "rank":          slip.get("rank", 1),
        "legs":          graded_legs,
        "legs_correct":  legs_win,
        "legs_graded":   len(decided),
        "slip_win":      slip_win,
        "combined_prob": slip.get("combined_prob"),
    }


def grade_slips(date_str, results_by_pid):
    """Grade each saved slip's legs; record per-rank win/loss for cross-date comparison."""
    slips_path = os.path.join(slips_mod.SLIPS_DIR, f"{date_str}.json")
    if not os.path.exists(slips_path):
        print(f"No slips recorded for {date_str}. Skipping.")
        return

    with open(slips_path, encoding="utf-8") as f:
        slips_data = json.load(f)

    all_sr = load_json(SLIPS_RESULTS_PATH, {"dates": {}, "records": {}})
    all_sr.setdefault("dates", {})
    all_sr.setdefault("records", {})

    date_results = {}
    for slip_key, slip_list in slips_data.get("slips", {}).items():
        platform = slip_key.split("_")[0]
        # Tolerate old single-slip format (dict) gracefully
        if isinstance(slip_list, dict):
            slip_list = [slip_list] if slip_list else []
        if not slip_list:
            continue

        graded_list = []
        for slip in slip_list:
            if not slip:
                continue
            graded = _grade_single_slip(slip, platform, results_by_pid)
            graded_list.append(graded)

            rank    = graded["rank"]
            rec_key = f"{slip_key}_{rank}"
            rec = all_sr["records"].setdefault(
                rec_key, {"wins": 0, "losses": 0, "legs_win": 0, "legs_total": 0, "expected_wins": 0.0}
            )
            rec.setdefault("expected_wins", 0.0)
            rec["legs_win"]   += graded["legs_correct"]
            rec["legs_total"] += graded["legs_graded"]
            if graded["slip_win"]:
                rec["wins"] += 1
            elif graded["legs_graded"] > 0:
                rec["losses"] += 1
            # expected_wins accumulates the slip's own recorded combined_prob
            # (from its legs' win_prob at publish time) for every slip that
            # was actually decided - so a 0-X record reads against what the
            # model itself expected, not against an implicit 50/50 or 100%
            # assumption. Only counted once legs are graded, matching wins/losses.
            if graded["legs_graded"] > 0 and graded.get("combined_prob") is not None:
                rec["expected_wins"] += graded["combined_prob"]

            icon = "WIN" if graded["slip_win"] else "LOSS"
            print(f"  Slip {rec_key}: {graded['legs_correct']}/{graded['legs_graded']} legs [{icon}]")

        date_results[slip_key] = graded_list

    all_sr["dates"][date_str] = date_results
    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(SLIPS_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(all_sr, f, indent=2)


def update_all_results(date_str, summary, results):
    all_results = load_json(ALL_RESULTS_PATH, {"dates": {}, "players": {}})
    all_results.setdefault("dates", {})
    all_results.setdefault("players", {})

    all_results["dates"][date_str] = {
        "ud": summary["ud"],
        "pp": summary["pp"],
        "player_count": len(results),
    }

    for r in results:
        pid = str(r["player_id"])
        entry = all_results["players"].setdefault(pid, {"name": r["name"], "team": r["team"], "history": []})
        entry["name"] = r["name"]
        entry["team"] = r["team"]
        entry["history"] = [h for h in entry["history"] if h["date"] != date_str]
        entry["history"].append({
            "date": date_str,
            "projected_ud": r["projected_ud"],
            "ud_line": r.get("ud_line"),
            "actual_ud": r["actual_ud"],
            "result_ud": r["result_ud"],
            "projected_pp": r["projected_pp"],
            "pp_line": r.get("pp_line"),
            "actual_pp": r["actual_pp"],
            "result_pp": r["result_pp"],
            "game_time_pt": r.get("game_time_pt"),
        })
        entry["history"].sort(key=lambda h: h["date"])

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(ALL_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)

    print(f"Updated -> {ALL_RESULTS_PATH}")


def _load_json_ids(path, extractor):
    """Loads path (if present) and returns the set of player_ids extractor
    pulls out of it. Used to compute that date's VALUE PLAY/SLIP/STACK
    tier-membership badges - these are date-specific (unlike premiumTier,
    which is derivable straight from the row), so they have to be read
    from that day's own saved output, not today's."""
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return extractor(data)
    except Exception:
        return set()


def track_top25(date_str, rows, results_by_pid):
    """Record the top-25 players from this date's dashboard, with their
    win/loss outcome vs the UD line (same directional grade as the rest of
    the dashboard), and keep a per-player running history of Top-25
    appearances."""
    top25 = sorted(rows, key=lambda r: r["ud_pts"], reverse=True)[:25]

    value_pids = _load_json_ids(
        os.path.join(VALUE_PLAYS_DIR, f"{date_str}.json"),
        lambda d: {p.get("player_id") for p in d.get("plays", [])},
    )
    slip_pids = _load_json_ids(
        os.path.join(slips_mod.SLIPS_DIR, f"{date_str}.json"),
        lambda d: {leg["player_id"] for slip_list in d.get("slips", {}).values()
                   for slip in slip_list for leg in slip["legs"]},
    )
    stack_pids = _load_json_ids(
        os.path.join(stacks_mod.STACKS_DIR, f"{date_str}.json"),
        lambda d: {leg["player_id"] for pairing in d.get("pairings", [])
                   for trio in pairing["trios"] for leg in trio["legs"]},
    )

    entries = []
    for row in top25:
        pid = row["player_id"]
        res = results_by_pid.get(pid)
        actual_ud = res["actual_ud"] if res else None
        grade = res["result_ud"] if res else None
        proj_ud_val = row["ud_pts"]
        ud_line = row.get("ud_line")
        graded_vs_proj = False

        # Fallback: when no UD market line is posted, grade against our own
        # projection as the benchmark so the player isn't silently excluded.
        if grade is None and actual_ud is not None:
            graded_vs_proj = True
            if actual_ud > proj_ud_val:
                grade = "win"
            elif actual_ud < proj_ud_val:
                grade = "loss"
            else:
                grade = "push"

        entries.append({
            "player_id": pid,
            "date": date_str,
            "name": row["name"],
            "team": row["team"],
            "order": row["order"] or "-",
            "ud": report.fmt_value(row["ud_pts"], "1f"),
            "pp": report.fmt_value(row["pp_pts"], "1f"),
            "xwoba": report.fmt_value(row["xwoba"], "3f"),
            "barrel": report.fmt_value(row["barrel_pct"], "1f"),
            "era": report.fmt_value(row["opp_era"], "2f"),
            "wxIcon": report.weather_icon(row),
            "wxText": row["weather"],
            "park": report.fmt_value(row["park_hr"], "2f"),
            "platoon": row["platoon_edge"] == "Yes",
            "adjusted": row.get("adjusted", False),
            "tier": report.card_tier(row["ud_pts"]),
            "gameTimePt": row.get("game_time_pt"),
            "projected_ud": proj_ud_val,
            "ud_line": ud_line,
            "actual_ud": actual_ud,
            "grade": grade,
            "graded_vs_proj": graded_vs_proj,
            "getaway_day_risk": row.get("getaway_day_risk", False),
            "premiumTier": slips_mod.premium_tier(row),
            "valuePlay": pid in value_pids,
            "slip": pid in slip_pids,
            "stack": pid in stack_pids,
        })

    top25_data = load_json(TOP25_RESULTS_PATH, {"dates": {}, "players": {}})
    top25_data.setdefault("dates", {})
    top25_data.setdefault("players", {})

    top25_data["dates"][date_str] = {"top25": entries}

    for entry in entries:
        pid = str(entry["player_id"])
        p = top25_data["players"].setdefault(pid, {
            "name": entry["name"], "team": entry["team"], "dates_seen": [], "history": [],
        })
        p["name"] = entry["name"]
        p["team"] = entry["team"]
        p.setdefault("dates_seen", [])
        if date_str not in p["dates_seen"]:
            p["dates_seen"].append(date_str)
            p["dates_seen"].sort()

        # Always purge any stale entry for this date first - a day with no
        # market-line coverage (grade is None for everyone) must not leave a
        # leftover graded entry from a previous run sitting in history.
        p["history"] = [h for h in p.get("history", []) if h["date"] != date_str]
        if entry["grade"] is not None:
            hist_entry = {
                "date": date_str,
                "projected_ud": entry["projected_ud"],
                "actual_ud": entry["actual_ud"],
                "grade": entry["grade"],
            }
            if entry.get("graded_vs_proj"):
                hist_entry["graded_vs_proj"] = True
            p["history"].append(hist_entry)
        p["history"].sort(key=lambda h: h["date"])

    with open(TOP25_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(top25_data, f, indent=2)

    decided = [e for e in entries if e["grade"] in ("win", "loss")]
    hits = sum(1 for e in decided if e["grade"] == "win")
    if decided:
        print(f"Top 25: {hits}/{len(decided)} win ({round(100*hits/len(decided),1)}%)")
    print(f"Updated -> {TOP25_RESULTS_PATH}")

    track_marked_plays(top25_data)


def _mark_tier(n_before, wins_before):
    """fire/hot/None classification from a player's Top-25 record STRICTLY
    BEFORE the date being classified - mirrors report.top25TreatmentClass's
    live badge logic (n>=8 minimum sample, 55%+ bar, n>=15 for fire vs.
    n 8-14 for hot) but point-in-time, so a date's classification never
    depends on results that hadn't happened yet when that day's picks went
    out."""
    if n_before <= 0:
        return None
    rate = wins_before / n_before
    if rate < MARK_MIN_RATE:
        return None
    if n_before >= FIRE_MIN_N:
        return "fire"
    if n_before >= HOT_MIN_N:
        return "hot"
    return None


def track_marked_plays(top25_data):
    """Fire/Hot marked-play cohort grading.

    Recomputed from scratch off top25_data's full player histories on every
    call (not accumulated incrementally), because these cohorts are DEFINED
    by a player's past hit rate: the only honest way to grade a given date
    is to reconstruct exactly which players would have carried the fire/hot
    treatment using ONLY their appearances strictly before that date (see
    _mark_tier). That also makes backfilling free - rerunning this against
    the existing top25_results.json reconstructs every date's marks from
    the historical record as it stood that day, with no risk of leaking a
    player's later results into their own earlier classification.

    UNVALIDATED cohort: it's selected on past performance, so it carries the
    same regression-to-the-mean risk that retired Premium (see slips.py /
    report.py PREMIUM history). Track and display it honestly, but it is
    not a proven edge until it accumulates real out-of-sample volume.
    """
    players = top25_data.get("players", {})
    histories = {pid: sorted(p.get("history", []), key=lambda h: h["date"])
                 for pid, p in players.items()}

    dates = sorted(top25_data.get("dates", {}).keys())
    out_dates = {}
    totals = {"fire": {"wins": 0, "losses": 0}, "hot": {"wins": 0, "losses": 0}}

    for d in dates:
        day_out = {"fire": [], "hot": []}
        for e in top25_data["dates"][d]["top25"]:
            pid = str(e["player_id"])
            before = [h for h in histories.get(pid, []) if h["date"] < d]
            wins_before = sum(1 for h in before if h["grade"] == "win")
            n_before = sum(1 for h in before if h["grade"] in ("win", "loss"))
            mark = _mark_tier(n_before, wins_before)
            if mark is None:
                continue
            grade = e.get("grade")
            day_out[mark].append({
                "player_id": e["player_id"],
                "name": e.get("name"),
                "team": e.get("team"),
                "grade": grade,
                "n_before": n_before,
                "wins_before": wins_before,
                "rate_before": round(100 * wins_before / n_before, 1),
            })
            if grade == "win":
                totals[mark]["wins"] += 1
            elif grade == "loss":
                totals[mark]["losses"] += 1
        if day_out["fire"] or day_out["hot"]:
            out_dates[d] = day_out

    cohorts = {}
    for mark in ("fire", "hot"):
        w, l = totals[mark]["wins"], totals[mark]["losses"]
        n = w + l
        cohorts[mark] = {"wins": w, "losses": l, "n": n,
                          "rate": round(100 * w / n, 1) if n else None}

    out = {"dates": out_dates, "cohorts": cohorts}
    with open(MARKED_PLAYS_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    for mark in ("fire", "hot"):
        c = cohorts[mark]
        if c["n"]:
            print(f"  Marked plays [{mark}]: {c['wins']}-{c['losses']} ({c['rate']}%, n={c['n']})")
        else:
            print(f"  Marked plays [{mark}]: no decided plays yet")
    print(f"Updated -> {MARKED_PLAYS_RESULTS_PATH}")
    return out


def find_ungraded_date():
    """Return the most recent past date that has projection data but no graded
    results yet, using the UTC calendar date as the 'today' boundary.

    This is immune to GitHub Actions cron delay: instead of deriving the target
    date from the current clock (fragile — breaks if the cron fires 7+ hours
    late), we scan what's actually ungraded. Games finish by ~5 AM UTC, so any
    date strictly before today's UTC date is guaranteed to have complete results.

    Returns None if the most recent past date is already fully graded (> 0
    players with results), which means nothing needs to be done.
    """
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Collect only top-level dated projection files (ignore subdirectories)
    data_files = sorted(
        [f for f in glob.glob(os.path.join("data", "2026-*.json"))
         if os.path.isfile(f)],
        reverse=True,
    )

    for fpath in data_files:
        date = os.path.basename(fpath).replace(".json", "")

        # Only grade completed days — any date still in UTC today has games
        # that may not have finished yet
        if date >= today_utc:
            continue

        results_path = os.path.join(RESULTS_DIR, f"results_{date}.json")
        if os.path.exists(results_path):
            try:
                with open(results_path, encoding="utf-8") as f:
                    rdata = json.load(f)
                if len(rdata.get("players", [])) > 0:
                    # Most recent past date is already graded — nothing to do
                    return None
            except Exception:
                pass  # treat a corrupt file as ungraded

        return date  # this is the date that needs grading

    return None


def main():
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
    else:
        date_str = find_ungraded_date()
        if date_str is None:
            print("All recent dates already graded — nothing to do.")
            return

    print(f"=== Tracking results for {date_str} ===")
    track_date(date_str)


if __name__ == "__main__":
    main()
