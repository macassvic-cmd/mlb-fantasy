"""
MLB Fantasy Results Tracker
Compares each player's projected UD/PP fantasy points (as shown on the
dashboard) against their actual box-score performance for a given date.
Also tracks hit/miss for the daily Top 25 specifically.

Usage:
  python tracker.py              # grades yesterday's games
  python tracker.py 2026-06-10   # grades a specific date
"""

import json
import os
import sys
from datetime import datetime, timedelta

import projections as proj
import report
from scrapers.market_lines import compute_pp_ud_ratio, load_cached_market_lines, match_lines
from scrapers.mlb_api import get_player_game_log

HIT_TOLERANCE = 0.20  # within 20% of projection counts as a "hit"

RESULTS_DIR = os.path.join("data", "results")
ALL_RESULTS_PATH = os.path.join(RESULTS_DIR, "all_results.json")
TOP25_RESULTS_PATH = os.path.join(RESULTS_DIR, "top25_results.json")
VALUE_PLAYS_DIR = os.path.join("data", "value_plays")
VALUE_PLAYS_RESULTS_PATH = os.path.join(RESULTS_DIR, "value_plays_results.json")


def classify(projected, actual):
    if projected <= 0:
        return "push" if actual <= 0 else "over"
    diff = actual - projected
    if abs(diff) <= HIT_TOLERANCE * projected:
        return "hit"
    return "over" if diff > 0 else "under"


def grade_top25(projected, actual):
    """Grade a Top-25 pick for the dashboard overlay/record table.
    Going over a projection is good for fantasy, so "exceeded or hit" is
    green, "close" is yellow, and "fell well short" is red.
      green  = actual >= 90% of projected (hit or exceeded)
      yellow = actual within 80-90% of projected (close)
      red    = actual < 80% of projected (significant miss)
    """
    if projected <= 0:
        return "green" if actual >= 0 else "red"
    ratio = actual / projected
    if ratio >= 0.9:
        return "green"
    if ratio >= 0.8:
        return "yellow"
    return "red"


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
    report.apply_corrections(rows, corrections)

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

        results.append({
            "player_id": player_id,
            "name": row["name"],
            "team": row["team"],
            "projected_ud": proj_ud,
            "actual_ud": actual_ud,
            "result_ud": classify(proj_ud, actual_ud),
            "projected_pp": proj_pp,
            "actual_pp": actual_pp,
            "result_pp": classify(proj_pp, actual_pp),
            "actual_line": {k: line[k] for k in
                            ("singles", "doubles", "triples", "hr", "bb", "hbp", "rbi", "runs", "sb")},
        })

    summary = summarize(results)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    out_path = os.path.join(RESULTS_DIR, f"results_{date_str}.json")
    payload = {"date": date_str, "summary": summary, "players": results}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"Saved {len(results)} results -> {out_path}")
    print(f"UD: {summary['ud']['hit_rate']}% hit "
          f"({summary['ud']['hit']} hit / {summary['ud']['over']} over / {summary['ud']['under']} under)")
    print(f"PP: {summary['pp']['hit_rate']}% hit "
          f"({summary['pp']['hit']} hit / {summary['pp']['over']} over / {summary['pp']['under']} under)")

    update_all_results(date_str, summary, results)

    results_by_pid = {r["player_id"]: r for r in results}
    track_top25(date_str, rows, results_by_pid)
    grade_value_plays(date_str, results_by_pid)

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
            "actual_ud": r["actual_ud"],
            "result_ud": r["result_ud"],
            "projected_pp": r["projected_pp"],
            "actual_pp": r["actual_pp"],
            "result_pp": r["result_pp"],
        })
        entry["history"].sort(key=lambda h: h["date"])

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(ALL_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2)

    print(f"Updated -> {ALL_RESULTS_PATH}")


def track_top25(date_str, rows, results_by_pid):
    """Record the top-25 players from this date's dashboard, with their
    hit/miss outcome, and keep a per-player running history of Top-25
    appearances."""
    top25 = sorted(rows, key=lambda r: r["ud_pts"], reverse=True)[:25]

    entries = []
    for row in top25:
        pid = row["player_id"]
        res = results_by_pid.get(pid)
        actual_ud = res["actual_ud"] if res else None
        grade = grade_top25(row["ud_pts"], actual_ud) if res else None
        entries.append({
            "player_id": pid,
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
            "projected_ud": row["ud_pts"],
            "actual_ud": actual_ud,
            "grade": grade,
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

        if entry["grade"] is not None:
            p["history"] = [h for h in p.get("history", []) if h["date"] != date_str]
            p["history"].append({
                "date": date_str,
                "projected_ud": entry["projected_ud"],
                "actual_ud": entry["actual_ud"],
                "grade": entry["grade"],
            })
            p["history"].sort(key=lambda h: h["date"])

    with open(TOP25_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(top25_data, f, indent=2)

    graded = sum(1 for e in entries if e["grade"] is not None)
    hits = sum(1 for e in entries if e["grade"] == "green")
    if graded:
        print(f"Top 25: {hits}/{graded} green ({round(100*hits/graded,1)}%)")
    print(f"Updated -> {TOP25_RESULTS_PATH}")


def main():
    if len(sys.argv) > 1:
        date_str = sys.argv[1]
    else:
        date_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"=== Tracking results for {date_str} ===")
    track_date(date_str)


if __name__ == "__main__":
    main()
