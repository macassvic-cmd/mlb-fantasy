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

RESULTS_DIR = os.path.join("data", "results")
ALL_RESULTS_PATH = os.path.join(RESULTS_DIR, "all_results.json")
TOP25_RESULTS_PATH = os.path.join(RESULTS_DIR, "top25_results.json")
VALUE_PLAYS_DIR = os.path.join("data", "value_plays")
VALUE_PLAYS_RESULTS_PATH = os.path.join(RESULTS_DIR, "value_plays_results.json")
SLIPS_RESULTS_PATH = os.path.join(RESULTS_DIR, "slips_results.json")


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
    grade_slips(date_str, results_by_pid)

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
        "rank":         slip.get("rank", 1),
        "legs":         graded_legs,
        "legs_correct": legs_win,
        "legs_graded":  len(decided),
        "slip_win":     slip_win,
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
                rec_key, {"wins": 0, "losses": 0, "legs_win": 0, "legs_total": 0}
            )
            rec["legs_win"]   += graded["legs_correct"]
            rec["legs_total"] += graded["legs_graded"]
            if graded["slip_win"]:
                rec["wins"] += 1
            elif graded["legs_graded"] > 0:
                rec["losses"] += 1

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


def track_top25(date_str, rows, results_by_pid):
    """Record the top-25 players from this date's dashboard, with their
    win/loss outcome vs the UD line (same directional grade as the rest of
    the dashboard), and keep a per-player running history of Top-25
    appearances."""
    top25 = sorted(rows, key=lambda r: r["ud_pts"], reverse=True)[:25]

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
