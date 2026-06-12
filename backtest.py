"""
MLB Fantasy Full-Season Backtest

For every date in a range, reconstructs the projections as they would have
appeared on that day's dashboard, grades them against actual box scores, and
accumulates results for model calibration.

Usage:
  python backtest.py                              # 2026-03-20 .. yesterday
  python backtest.py --start 2026-05-01
  python backtest.py --start 2026-05-01 --end 2026-05-31
  python backtest.py --limit 5                    # only process first N dates
  python backtest.py --no-cache                   # re-run pipeline even if data/<date>.json exists
  python backtest.py --calibrate-only             # skip the loop, just (re)run calibration
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta

import pipeline
import projections as proj
import report
from tracker import classify, summarize
from scrapers.mlb_api import get_player_game_log
from scrapers.market_lines import load_cached_market_lines, compute_pp_ud_ratio, match_lines

BACKTEST_DIR = os.path.join("data", "backtest")
ALL_BACKTEST_PATH = os.path.join(BACKTEST_DIR, "all_backtest.json")
SUMMARY_PATH = os.path.join(BACKTEST_DIR, "backtest_summary.json")
RESULTS_PATH = os.path.join("data", "results", "all_results.json")

CORRECTION_CLAMP = (0.8, 1.2)
MIN_GAMES_FOR_CORRECTION = 10


# ---------------------------------------------------------------------------
# Player-type classification
# ---------------------------------------------------------------------------

def classify_player_type(p, row):
    """Heuristic bucket: power / speed / leadoff / contact / other,
    based on season-to-date per-game rates and batting order."""
    season = p.get("season_stats") or {}
    g = season.get("games", 0) or 0
    order = row.get("order") or 9

    if g < 5:
        return "other"

    hr_pg = (season.get("hr", 0) or 0) / g
    sb_pg = (season.get("sb", 0) or 0) / g
    singles_pg = (season.get("singles", 0) or 0) / g

    if hr_pg >= 0.15:
        return "power"
    if sb_pg >= 0.08:
        return "speed"
    if order <= 2:
        return "leadoff"
    if singles_pg >= 0.30:
        return "contact"
    return "other"


# ---------------------------------------------------------------------------
# Per-date processing
# ---------------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def process_date(date_str, use_cache=True):
    data_path = os.path.join("data", f"{date_str}.json")

    if use_cache and os.path.exists(data_path):
        with open(data_path, encoding="utf-8") as f:
            players = json.load(f)
    else:
        try:
            players = pipeline.run_pipeline(date_str)
        except Exception as e:
            print(f"  pipeline failed for {date_str}: {e}")
            return None

    if not players:
        print(f"  no players for {date_str} (no games / lineups not posted) - skipping")
        return None

    rows = [report.build_row(p) for p in players]
    report.recalibrate_points(rows)

    # Market lines (UD/PP "Fantasy Points") are only cached for dates we ran
    # report.py on while the lines were live - historical dates won't have a
    # snapshot and market_ud/market_pp will be None for those.
    market_lines = load_cached_market_lines(date_str)
    pp_ud_ratio = compute_pp_ud_ratio(market_lines) if market_lines else None

    player_results = []
    for p, row in zip(players, rows):
        pid = row["player_id"]
        if not pid:
            continue

        try:
            line = get_player_game_log(pid, date_str)
        except Exception as e:
            print(f"  warning: game log failed for {row['name']}: {e}")
            line = None

        if not line or not line.get("games"):
            continue  # didn't play

        actual_ud = proj._score(line, proj.UD_SCORING)
        actual_pp = proj._score(line, proj.PP_SCORING)
        proj_ud = row["ud_pts"]
        proj_pp = row["pp_pts"]

        market_ud = market_pp = None
        if market_lines:
            market_ud, market_pp = match_lines(row["name"], market_lines, pp_ud_ratio)

        player_results.append({
            "player_id": pid,
            "name": row["name"],
            "team": row["team"],
            "projected_ud": proj_ud,
            "actual_ud": actual_ud,
            "result_ud": classify(proj_ud, actual_ud),
            "projected_pp": proj_pp,
            "actual_pp": actual_pp,
            "result_pp": classify(proj_pp, actual_pp),
            "market_ud": market_ud,
            "market_pp": market_pp,
            "signals": {
                "xwoba": row["xwoba"],
                "barrel_pct": row["barrel_pct"],
                "opp_era": row["opp_era"],
                "park_hr": row["park_hr"],
                "platoon_edge": row["platoon_edge"],
                "order": row["order"],
                "wx_temp": row["wx_temp"],
            },
            "player_type": classify_player_type(p, row),
        })

    if not player_results:
        print(f"  no graded players for {date_str} - skipping")
        return None

    summary = summarize(player_results)

    os.makedirs(BACKTEST_DIR, exist_ok=True)
    out_path = os.path.join(BACKTEST_DIR, f"backtest_{date_str}.json")
    payload = {"date": date_str, "summary": summary, "players": player_results}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"  graded {len(player_results)} players | "
          f"UD hit rate {summary['ud']['hit_rate']}% | PP hit rate {summary['pp']['hit_rate']}%")

    update_all_backtest(date_str, summary, player_results)
    return payload


def update_all_backtest(date_str, summary, player_results):
    all_data = load_json(ALL_BACKTEST_PATH, {"dates": {}, "players": {}})
    all_data.setdefault("dates", {})
    all_data.setdefault("players", {})

    all_data["dates"][date_str] = {
        "ud": summary["ud"],
        "pp": summary["pp"],
        "player_count": len(player_results),
    }

    for r in player_results:
        pid = str(r["player_id"])
        entry = all_data["players"].setdefault(pid, {"name": r["name"], "team": r["team"], "history": []})
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
            "market_ud": r.get("market_ud"),
            "market_pp": r.get("market_pp"),
            "signals": r["signals"],
            "player_type": r["player_type"],
        })
        entry["history"].sort(key=lambda h: h["date"])

    os.makedirs(BACKTEST_DIR, exist_ok=True)
    with open(ALL_BACKTEST_PATH, "w", encoding="utf-8") as f:
        json.dump(all_data, f, indent=2)


# ---------------------------------------------------------------------------
# Calibration analysis
# ---------------------------------------------------------------------------

def pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return 0.0
    return round(cov / (var_x ** 0.5 * var_y ** 0.5), 3)


def run_calibration():
    if not os.path.exists(ALL_BACKTEST_PATH):
        print("No backtest data found - run the backtest loop first.")
        return

    with open(ALL_BACKTEST_PATH, encoding="utf-8") as f:
        data = json.load(f)

    players = data.get("players", {})
    dates = data.get("dates", {})

    if not dates:
        print("No graded dates in all_backtest.json.")
        return

    # --- Overall hit rate -------------------------------------------------
    total_hit = total = 0
    for d in dates.values():
        total_hit += d["ud"]["hit"]
        total += d["ud"]["total"]
    overall_hit_rate = round(100 * total_hit / total, 1) if total else 0.0

    # --- Global scale factor: avg actual / avg projected across all games --
    all_history = [h for p in players.values() for h in p.get("history", [])]
    n_games = len(all_history)
    avg_proj_ud = sum(h["projected_ud"] for h in all_history) / n_games
    avg_actual_ud = sum(h["actual_ud"] for h in all_history) / n_games
    avg_proj_pp = sum(h["projected_pp"] for h in all_history) / n_games
    avg_actual_pp = sum(h["actual_pp"] for h in all_history) / n_games
    global_scale = {
        "ud": round(avg_actual_ud / avg_proj_ud, 4),
        "pp": round(avg_actual_pp / avg_proj_pp, 4),
        "avg_projected_ud": round(avg_proj_ud, 3),
        "avg_actual_ud": round(avg_actual_ud, 3),
        "avg_projected_pp": round(avg_proj_pp, 3),
        "avg_actual_pp": round(avg_actual_pp, 3),
        "n_games": n_games,
    }

    # --- Per-player correction factors (>= MIN_GAMES_FOR_CORRECTION) ------
    lo, hi = CORRECTION_CLAMP
    corrections = {}
    for pid, p in players.items():
        history = p.get("history", [])
        if len(history) < MIN_GAMES_FOR_CORRECTION:
            continue
        ud_proj = sum(h["projected_ud"] for h in history)
        ud_actual = sum(h["actual_ud"] for h in history)
        pp_proj = sum(h["projected_pp"] for h in history)
        pp_actual = sum(h["actual_pp"] for h in history)
        ud_factor = ud_actual / ud_proj if ud_proj > 0 else 1.0
        pp_factor = pp_actual / pp_proj if pp_proj > 0 else 1.0
        corrections[pid] = {
            "name": p.get("name", ""),
            "team": p.get("team", ""),
            "ud": round(min(max(ud_factor, lo), hi), 3),
            "pp": round(min(max(pp_factor, lo), hi), 3),
            "games": len(history),
        }

    # --- Market-line correction factors (>= MIN_GAMES_FOR_CORRECTION) -----
    # How much each player has historically beaten/missed their own posted
    # UD/PP "Fantasy Points" line. Only covers dates where a market line
    # snapshot was cached (see load_cached_market_lines), so this starts
    # empty and fills in as dates are graded going forward.
    market_corrections = {}
    market_games = 0
    for pid, p in players.items():
        history = [h for h in p.get("history", []) if h.get("market_ud") is not None]
        market_games += len(history)
        if len(history) < MIN_GAMES_FOR_CORRECTION:
            continue
        ud_market = sum(h["market_ud"] for h in history)
        ud_actual = sum(h["actual_ud"] for h in history)
        pp_market = sum(h["market_pp"] for h in history)
        pp_actual = sum(h["actual_pp"] for h in history)
        ud_factor = ud_actual / ud_market if ud_market > 0 else 1.0
        pp_factor = pp_actual / pp_market if pp_market > 0 else 1.0
        market_corrections[pid] = {
            "name": p.get("name", ""),
            "team": p.get("team", ""),
            "ud": round(min(max(ud_factor, lo), hi), 3),
            "pp": round(min(max(pp_factor, lo), hi), 3),
            "games": len(history),
        }

    # Persist correction factors into all_results.json so the live model
    # (report.build_corrections / apply_market_anchor) picks them up immediately.
    results_data = load_json(RESULTS_PATH, {"dates": {}, "players": {}})
    results_data["backtest_corrections"] = corrections
    results_data["market_corrections"] = market_corrections
    results_data["global_scale"] = global_scale
    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(results_data, f, indent=2)

    # --- Signal correlation with "hit" --------------------------------------
    numeric_signals = ["xwoba", "barrel_pct", "opp_era", "park_hr", "order", "wx_temp"]
    signal_correlation = {}
    for sig in numeric_signals:
        xs, ys = [], []
        for p in players.values():
            for h in p.get("history", []):
                v = (h.get("signals") or {}).get(sig)
                if v is None:
                    continue
                xs.append(float(v))
                ys.append(1.0 if h["result_ud"] == "hit" else 0.0)
        signal_correlation[sig] = {
            "correlation": pearson(xs, ys) if len(xs) >= 10 else None,
            "n": len(xs),
        }

    # --- Platoon edge hit rate --------------------------------------------
    platoon_hit_rate = {}
    for cat in ("Yes", "No", "N/A"):
        hits = n = 0
        for p in players.values():
            for h in p.get("history", []):
                if (h.get("signals") or {}).get("platoon_edge") == cat:
                    n += 1
                    if h["result_ud"] == "hit":
                        hits += 1
        platoon_hit_rate[cat] = {"hit_rate": round(100 * hits / n, 1) if n else None, "n": n}

    # --- Player-type hit rate ----------------------------------------------
    type_hit_rate = {}
    for ptype in ("power", "speed", "leadoff", "contact", "other"):
        hits = n = 0
        for p in players.values():
            for h in p.get("history", []):
                if h.get("player_type") == ptype:
                    n += 1
                    if h["result_ud"] == "hit":
                        hits += 1
        if n:
            type_hit_rate[ptype] = {"hit_rate": round(100 * hits / n, 1), "n": n}

    # --- Monthly hit rate ----------------------------------------------------
    monthly = {}
    for date_str, d in dates.items():
        month = date_str[:7]
        m = monthly.setdefault(month, {"hit": 0, "total": 0})
        m["hit"] += d["ud"]["hit"]
        m["total"] += d["ud"]["total"]
    monthly_hit_rate = {
        m: {"hit_rate": round(100 * v["hit"] / v["total"], 1), "n": v["total"]}
        for m, v in sorted(monthly.items()) if v["total"]
    }

    # --- Signal-strength buckets (terciles) ---------------------------------
    signal_buckets = {}
    for sig in ("xwoba", "barrel_pct"):
        vals = []
        for p in players.values():
            for h in p.get("history", []):
                v = (h.get("signals") or {}).get(sig)
                if v is not None:
                    vals.append((float(v), h["result_ud"] == "hit"))
        if len(vals) >= 10:
            vals.sort(key=lambda x: x[0])
            n = len(vals)
            t1, t2 = n // 3, 2 * n // 3
            low, mid, high_ = vals[:t1], vals[t1:t2], vals[t2:]
            signal_buckets[sig] = {
                "low":    {"hit_rate": round(100 * sum(1 for _, hit in low if hit) / len(low), 1), "n": len(low)} if low else None,
                "medium": {"hit_rate": round(100 * sum(1 for _, hit in mid if hit) / len(mid), 1), "n": len(mid)} if mid else None,
                "high":   {"hit_rate": round(100 * sum(1 for _, hit in high_ if hit) / len(high_), 1), "n": len(high_)} if high_ else None,
            }

    # --- Top 10 most / least accurate players (>= MIN_GAMES_FOR_CORRECTION) -
    player_accuracy = []
    for pid, p in players.items():
        history = p.get("history", [])
        if len(history) < MIN_GAMES_FOR_CORRECTION:
            continue
        hits = sum(1 for h in history if h["result_ud"] == "hit")
        player_accuracy.append({
            "name": p.get("name", ""),
            "team": p.get("team", ""),
            "hit_rate": round(100 * hits / len(history), 1),
            "games": len(history),
        })
    player_accuracy.sort(key=lambda x: (x["hit_rate"], x["games"]), reverse=True)
    top10_best = player_accuracy[:10]
    top10_worst = list(reversed(player_accuracy[-10:])) if len(player_accuracy) > 10 else list(reversed(player_accuracy))

    summary_out = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "date_range": [min(dates.keys()), max(dates.keys())],
        "dates_graded": len(dates),
        "overall_hit_rate": overall_hit_rate,
        "total_player_games": total,
        "global_scale": global_scale,
        "monthly_hit_rate": monthly_hit_rate,
        "player_type_hit_rate": type_hit_rate,
        "signal_correlation": signal_correlation,
        "signal_buckets": signal_buckets,
        "platoon_hit_rate": platoon_hit_rate,
        "top10_best": top10_best,
        "top10_worst": top10_worst,
        "correction_count": len(corrections),
        "market_correction_count": len(market_corrections),
        "market_lines_player_games": market_games,
    }

    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary_out, f, indent=2)

    print(f"\n=== Calibration complete ===")
    print(f"Dates graded: {len(dates)}  ({summary_out['date_range'][0]} .. {summary_out['date_range'][1]})")
    print(f"Overall UD hit rate: {overall_hit_rate}% across {total} player-games")
    print(f"Correction factors computed for {len(corrections)} players -> {RESULTS_PATH}")
    print(f"Market-line correction factors computed for {len(market_corrections)} players "
          f"({market_games} player-games had a cached market line)")
    print(f"Summary saved -> {SUMMARY_PATH}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def daterange(start_str, end_str):
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    dates = []
    d = start
    while d <= end:
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return dates


def main():
    parser = argparse.ArgumentParser(description="MLB Fantasy full-season backtest")
    parser.add_argument("--start", default="2026-03-20")
    parser.add_argument("--end", default=None, help="default: yesterday")
    parser.add_argument("--limit", type=int, default=None, help="only process first N dates")
    parser.add_argument("--no-cache", action="store_true", help="re-run pipeline even if data/<date>.json exists")
    parser.add_argument("--calibrate-only", action="store_true", help="skip the date loop, just run calibration")
    args = parser.parse_args()

    if args.calibrate_only:
        run_calibration()
        return

    end = args.end or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    dates = daterange(args.start, end)
    if args.limit:
        dates = dates[:args.limit]

    print(f"=== Backtesting {len(dates)} dates: {dates[0]} .. {dates[-1]} ===")
    for i, date_str in enumerate(dates, 1):
        print(f"[{i}/{len(dates)}] {date_str}")
        try:
            process_date(date_str, use_cache=not args.no_cache)
        except Exception as e:
            print(f"  ERROR: {e}")
        time.sleep(0.5)

    print("\nBacktest loop complete. Running calibration analysis...")
    run_calibration()


if __name__ == "__main__":
    main()
