"""Closing Line Value (CLV) tracking - forward capture + grading.

CLV measures whether the line moved in our favor between when we'd surface
a play and when it locks (game start): took an OVER at 6.5, closed at 7.5 =
positive CLV, independent of whether the play itself won or lost. It's the
one piece of infrastructure this end-of-season pass adds beyond sharpening
existing signals (see report.py's is_actionable / shrinkage.py) - added
2026-09-13 specifically so the ~2 remaining weeks of the season produce
clean forward CLV data for the offseason deep-analysis, on top of whatever
thin backtest coverage genuinely exists (see backtest.backtest_clv).

Deliberately NOT a new calibrated-projection or EV model - it only measures
market movement on the plays the rest of the pipeline already decided were
actionable; it doesn't change what gets surfaced or how anything is priced.
"""

import json
import os
from datetime import datetime, timezone

SNAPSHOTS_DIR = os.path.join("data", "clv_snapshots")
CLV_RESULTS_PATH = os.path.join("data", "results", "clv_results.json")


def _snapshot_path(date_str):
    return os.path.join(SNAPSHOTS_DIR, f"{date_str}.json")


def _call_from_edge(edge):
    if edge is None:
        return None
    if edge > 0.5:
        return "over"
    if edge < -0.5:
        return "under"
    return None  # neutral - no directional call, nothing to grade for CLV


def record_line_snapshot(date_str, rows):
    """Update today's line-snapshot file with the current UD line for every
    market-anchored row. Called on every dashboard render (report.py's
    write_dashboard already runs on every pipeline cycle - roughly every 20
    minutes through the morning window plus two afternoon backups plus the
    nightly rebuild - so this needs no cron of its own).

    open_line/open_at are set only the FIRST time a player_id is seen that
    date; close_line/close_at/call/actionable_at_close are overwritten every
    call, so whichever render happens last before lock naturally becomes the
    closing snapshot. Only rows with a real posted line are captured - no
    line, no CLV to measure."""
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    path = _snapshot_path(date_str)
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    now_iso = datetime.now(timezone.utc).isoformat()
    for row in rows:
        if not row.get("market_anchored"):
            continue
        pid = row.get("player_id")
        ud_line = row.get("ud_line")
        if pid is None or ud_line is None:
            continue
        pid = str(pid)
        edge = row.get("edge")
        entry = data.get(pid)
        if entry is None:
            entry = {
                "name": row.get("name"),
                "team": row.get("team"),
                "market": "ud",
                "open_line": ud_line,
                "open_edge": edge,
                "open_at": now_iso,
            }
            data[pid] = entry
        entry["close_line"] = ud_line
        entry["close_edge"] = edge
        entry["close_at"] = now_iso
        entry["call"] = _call_from_edge(edge)
        entry["actionable_at_close"] = bool(row.get("actionable"))

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def compute_clv(open_line, close_line, call):
    """Signed CLV in line points: positive = line moved our way.
    OVER: line rising after we took it means the book now needs MORE than
    we needed, i.e. our number looks better in hindsight -> positive.
    UNDER: line falling after we took it is the same effect in reverse."""
    direction = 1 if call == "over" else -1
    return direction * (close_line - open_line)


def grade_clv(date_str):
    """Grade the day's forward CLV from its snapshot file (called nightly
    from tracker.py alongside the other grade_* functions). Only plays that
    were actionable at close (see report.is_actionable) and had a real
    directional call count - a neutral-edge line or a projected-lineup play
    that never confirmed isn't one of "the plays we'd surface", so it isn't
    part of this record. Like every other results file in this codebase,
    `record` is fully recomputed from `dates` on every run, never
    accumulated incrementally."""
    path = _snapshot_path(date_str)
    if not os.path.exists(path):
        print(f"CLV: no snapshot for {date_str} (dashboard wasn't generated that day?) - skipping.")
        return

    with open(path, encoding="utf-8") as f:
        snapshot = json.load(f)

    plays = []
    for pid, entry in snapshot.items():
        if not entry.get("actionable_at_close") or entry.get("call") not in ("over", "under"):
            continue
        open_line = entry.get("open_line")
        close_line = entry.get("close_line")
        if open_line is None or close_line is None:
            continue
        clv_points = compute_clv(open_line, close_line, entry["call"])
        plays.append({
            "player_id": pid,
            "name": entry.get("name"),
            "team": entry.get("team"),
            "call": entry["call"],
            "open_line": open_line,
            "open_at": entry.get("open_at"),
            "close_line": close_line,
            "close_at": entry.get("close_at"),
            "clv_points": round(clv_points, 2),
        })

    data = {"dates": {}, "record": {"positive": 0, "negative": 0, "push": 0, "n": 0, "win_rate": None, "avg_clv_points": None}}
    if os.path.exists(CLV_RESULTS_PATH):
        try:
            with open(CLV_RESULTS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    data.setdefault("dates", {})
    data["dates"][date_str] = {"plays": plays}

    all_plays = [p for dd in data["dates"].values() for p in dd["plays"]]
    positive = sum(1 for p in all_plays if p["clv_points"] > 0)
    negative = sum(1 for p in all_plays if p["clv_points"] < 0)
    push = sum(1 for p in all_plays if p["clv_points"] == 0)
    n = len(all_plays)
    data["record"] = {
        "positive": positive,
        "negative": negative,
        "push": push,
        "n": n,
        "win_rate": round(100 * positive / (positive + negative), 1) if (positive + negative) else None,
        "avg_clv_points": round(sum(p["clv_points"] for p in all_plays) / n, 3) if n else None,
    }

    os.makedirs(os.path.dirname(CLV_RESULTS_PATH), exist_ok=True)
    with open(CLV_RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    today_pos = sum(1 for p in plays if p["clv_points"] > 0)
    today_neg = sum(1 for p in plays if p["clv_points"] < 0)
    print(f"CLV: {today_pos}/{len(plays)} positive today -> running record "
          f"{positive}-{negative}-{push} push (n={n})"
          + (f", {data['record']['win_rate']}% positive, avg {data['record']['avg_clv_points']:+.3f} pts" if n else ""))
