"""Closing Line Value (CLV) tracking - forward capture + grading.

CLV measures whether the line moved in our favor between when we'd surface
a play and when it locks (game start): took an OVER at 6.5, closed at 7.5 =
positive CLV, independent of whether the play itself won or lost.

WHY THIS WAS REWRITTEN (2026-09-29, offseason)
----------------------------------------------
The 2026 version recorded "open" and "close" from the ud_line on the
dashboard rows at render time. Those rows get their line from
scrapers.market_lines.get_market_lines, which caches the UD board ONCE per
date and serves the cached copy to every later render (it only refetches
if the cached board is thin). So every render of the day saw the same
line, open always equalled close, and the season's forward record came out
607 pushes in 608 plays - an instrumentation failure, not a market finding.

v2 SCHEMA - separate the two things that were conflated:

  1. LINE OBSERVATIONS come only from LIVE fetches of the UD board
     (capture_live_lines, driven by clv_snapshot.py from a workflow step
     that runs on EVERY cron fire, before and independent of the pipeline's
     freshness gate). Each observation appends a {at, line} sample to the
     player's `samples` list whenever the line differs from the last sample
     seen; `last_seen_at` is updated on every observation so it's provable
     that the board was still being watched right up to lock. Observations
     after a player's lock time are ignored.

  2. PLAY ANNOTATIONS (which rows were actionable, the call direction, the
     edge) still come from the dashboard render (record_line_snapshot),
     but they no longer carry a line. They tell grade_clv WHICH players to
     grade; the samples tell it what the line did.

grade_clv then uses open = first sample, close = last sample at or before
lock. A play with a single sample is reported as a push but is counted
separately (plays_single_sample) so "the line never moved" can't be
confused with "we never looked twice" again.

Per-date snapshot file (data/clv_snapshots/{date}.json):
  {
    "version": 2, "date": "...",
    "players": {
      "<normalized name>": {
        "name": str, "player_id": int|None, "team": str|None,
        "lock_at": iso|None,
        "samples": [{"at": iso, "line": float}, ...],
        "last_seen_at": iso, "last_seen_line": float
      }
    },
    "annotations": {
      "<normalized name>": {
        "player_id", "name", "team", "call", "edge", "actionable",
        "lock_at", "annotated_at"
      }
    }
  }

Files written by the 2026 code (a flat {player_id: {open_line, close_line,
...}} dict with no "version" key) are still readable by grade_clv via the
legacy path, so re-grading a 2026 date reproduces its original number.

Deliberately NOT a new calibrated-projection or EV model - it only measures
market movement on the plays the rest of the pipeline already decided were
actionable; it doesn't change what gets surfaced or how anything is priced.
"""

import json
import logging
import os
from datetime import datetime, timezone

from scrapers.market_lines import fetch_underdog_mlb_lines, normalize_name

logger = logging.getLogger(__name__)

SNAPSHOTS_DIR = os.path.join("data", "clv_snapshots")
CLV_RESULTS_PATH = os.path.join("data", "results", "clv_results.json")
DATA_DIR = "data"  # per-date pipeline output, data/{date}.json - used for lock times / ids

SNAPSHOT_VERSION = 2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _snapshot_path(date_str):
    return os.path.join(SNAPSHOTS_DIR, f"{date_str}.json")


def _now(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now


def _iso(dt):
    return _now(dt).isoformat()


def _parse_iso(s):
    if not s:
        return None
    try:
        return _now(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        return None


def _call_from_edge(edge):
    if edge is None:
        return None
    if edge > 0.5:
        return "over"
    if edge < -0.5:
        return "under"
    return None  # neutral - no directional call, nothing to grade for CLV


def _empty_snapshot(date_str):
    return {"version": SNAPSHOT_VERSION, "date": date_str, "players": {}, "annotations": {}}


def load_snapshot(date_str):
    """Return the snapshot dict for date_str, or None if there is no file.
    A legacy (2026, versionless) file is returned as-is - callers check
    is_legacy_snapshot()."""
    path = _snapshot_path(date_str)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def is_legacy_snapshot(snapshot):
    return isinstance(snapshot, dict) and snapshot.get("version") != SNAPSHOT_VERSION


def _load_or_new(date_str):
    snap = load_snapshot(date_str)
    if snap is None or is_legacy_snapshot(snap):
        # Never mix v2 samples into a legacy file - a legacy file only exists
        # for 2026 dates, which no live capture should ever target again.
        if snap is not None:
            logger.warning(f"CLV: {date_str} has a legacy (v1) snapshot - starting a fresh v2 file alongside it is "
                           f"not supported; refusing to overwrite. Delete it first if you really mean to.")
            return None
        snap = _empty_snapshot(date_str)
    snap.setdefault("players", {})
    snap.setdefault("annotations", {})
    return snap


def _save(date_str, snapshot):
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    with open(_snapshot_path(date_str), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)


def _players_index_from_data(date_str):
    """{normalized name: {player_id, team, name, lock_at}} from the day's
    pipeline output, so a live observation can be tagged with the player's
    id and game lock time. Empty dict if the pipeline hasn't produced
    data/{date}.json yet (early-morning fires) - samples are still stored
    by name and get their lock_at filled on a later capture."""
    path = os.path.join(DATA_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            players = json.load(f)
    except Exception:
        return {}
    index = {}
    for p in players if isinstance(players, list) else []:
        name = p.get("name")
        if not name:
            continue
        index[normalize_name(name)] = {
            "player_id": p.get("player_id"),
            "team": p.get("team_name"),
            "name": name,
            "lock_at": p.get("game_date_utc"),
        }
    return index


# ---------------------------------------------------------------------------
# 1. live line observations
# ---------------------------------------------------------------------------

def capture_live_lines(date_str, lines=None, fetch=None, now=None):
    """Observe the live UD board once and fold it into the day's snapshot.

    `lines` - {normalized name: line}; fetched via `fetch` (default: the
    real UD fetch) when not supplied. Tests and the --lines-json CLI path
    inject it directly.

    A sample is appended only when the line differs from the player's last
    recorded sample (so a board polled 40x a day with no movement stays one
    sample, not forty), but last_seen_at/last_seen_line are updated on
    every observation. Observations at or after the player's lock time are
    ignored - UD normally pulls a line at first pitch, but if one lingers,
    a post-lock reading must not become "close".

    Returns a summary dict; never raises on fetch failure (the workflow
    step that calls this must not fail the job)."""
    now = _now(now)
    summary = {"date": date_str, "at": _iso(now), "fetched": False, "n_lines": 0,
               "n_new_samples": 0, "n_players": 0, "n_players_multi_sample": 0, "error": None}

    if lines is None:
        fetch = fetch or fetch_underdog_mlb_lines
        try:
            lines = fetch()
        except Exception as e:  # network / parse failure - report, don't raise
            summary["error"] = f"{type(e).__name__}: {e}"
            logger.warning(f"CLV: live UD fetch failed for {date_str}: {summary['error']}")
            return summary
    summary["fetched"] = True
    summary["n_lines"] = len(lines)

    snap = _load_or_new(date_str)
    if snap is None:
        summary["error"] = "legacy snapshot present"
        return summary
    index = _players_index_from_data(date_str)
    players = snap["players"]

    for key, line in lines.items():
        try:
            line = float(line)
        except (TypeError, ValueError):
            continue
        meta = index.get(key, {})
        entry = players.get(key)
        if entry is None:
            entry = {"name": meta.get("name") or key, "player_id": meta.get("player_id"),
                     "team": meta.get("team"), "lock_at": meta.get("lock_at"),
                     "samples": [], "last_seen_at": None, "last_seen_line": None}
            players[key] = entry
        else:
            # fill metadata that an earlier (pre-pipeline) capture couldn't know
            for k in ("player_id", "team", "lock_at"):
                if entry.get(k) is None and meta.get(k) is not None:
                    entry[k] = meta[k]
            if meta.get("name") and entry.get("name") == key:
                entry["name"] = meta["name"]

        lock_at = _parse_iso(entry.get("lock_at"))
        if lock_at is not None and now >= lock_at:
            continue  # post-lock reading - not part of the open->close window

        samples = entry["samples"]
        if not samples or samples[-1]["line"] != line:
            samples.append({"at": _iso(now), "line": line})
            summary["n_new_samples"] += 1
        entry["last_seen_at"] = _iso(now)
        entry["last_seen_line"] = line

    summary["n_players"] = len(players)
    summary["n_players_multi_sample"] = sum(1 for e in players.values() if len(e.get("samples", [])) >= 2)
    _save(date_str, snap)
    return summary


# ---------------------------------------------------------------------------
# 2. play annotations (from the dashboard render)
# ---------------------------------------------------------------------------

def record_line_snapshot(date_str, rows, now=None):
    """Called on every dashboard render (report.write_dashboard). Records,
    per market-anchored row, WHICH players are plays and in which direction
    - call, edge, actionable - keyed by normalized name so grade_clv can
    join them to the live samples. Carries NO line value on purpose: the
    row's ud_line is the pipeline's once-a-day cached board, which is
    exactly what made the 2026 record meaningless (see module docstring).

    Annotations are overwritten by each render up to the player's lock
    time, so the last pre-lock render decides call/actionable (matching
    the "actionable at close" rule grade_clv applies). A render after lock
    leaves the existing annotation alone."""
    now = _now(now)
    snap = _load_or_new(date_str)
    if snap is None:
        return
    annotations = snap["annotations"]
    for row in rows:
        if not row.get("market_anchored"):
            continue
        name = row.get("name")
        if not name or row.get("ud_line") is None:
            continue
        key = normalize_name(name)
        lock_at = row.get("game_date_utc")
        lock_dt = _parse_iso(lock_at)
        if key in annotations and lock_dt is not None and now >= lock_dt:
            continue
        edge = row.get("edge")
        annotations[key] = {
            "player_id": row.get("player_id"),
            "name": name,
            "team": row.get("team"),
            "call": _call_from_edge(edge),
            "edge": edge,
            "actionable": bool(row.get("actionable")),
            "lock_at": lock_at,
            "annotated_at": _iso(now),
        }
        # the render knows the id/lock time even when the live capture ran
        # before the pipeline did - backfill the sample entry's metadata
        entry = snap["players"].get(key)
        if entry is not None:
            if entry.get("player_id") is None and row.get("player_id") is not None:
                entry["player_id"] = row["player_id"]
            if entry.get("lock_at") is None and lock_at:
                entry["lock_at"] = lock_at
            if entry.get("team") is None and row.get("team"):
                entry["team"] = row["team"]
    _save(date_str, snap)


# ---------------------------------------------------------------------------
# 3. grading
# ---------------------------------------------------------------------------

def compute_clv(open_line, close_line, call):
    """Signed CLV in line points: positive = line moved our way.
    OVER: line rising after we took it means the book now needs MORE than
    we needed, i.e. our number looks better in hindsight -> positive.
    UNDER: line falling after we took it is the same effect in reverse."""
    direction = 1 if call == "over" else -1
    return direction * (close_line - open_line)


def open_close_samples(entry):
    """(open_sample, close_sample) for a v2 player entry: open = first
    sample, close = last sample at or before lock_at (or simply the last
    sample when lock time is unknown). None, None if there are no samples."""
    samples = entry.get("samples") or []
    if not samples:
        return None, None
    lock_at = _parse_iso(entry.get("lock_at"))
    close = None
    if lock_at is not None:
        for s in samples:
            at = _parse_iso(s.get("at"))
            if at is None or at <= lock_at:
                close = s
    if close is None:
        close = samples[-1]
    return samples[0], close


def _plays_from_v2(snapshot):
    plays, skipped_no_samples = [], 0
    players = snapshot.get("players", {})
    for key, ann in snapshot.get("annotations", {}).items():
        if not ann.get("actionable") or ann.get("call") not in ("over", "under"):
            continue
        entry = players.get(key)
        open_s, close_s = open_close_samples(entry) if entry else (None, None)
        if open_s is None:
            skipped_no_samples += 1
            continue
        plays.append({
            "player_id": ann.get("player_id") if ann.get("player_id") is not None else entry.get("player_id"),
            "name": ann.get("name") or entry.get("name"),
            "team": ann.get("team") or entry.get("team"),
            "call": ann["call"],
            "open_line": open_s["line"],
            "open_at": open_s["at"],
            "close_line": close_s["line"],
            "close_at": close_s["at"],
            "last_seen_at": entry.get("last_seen_at"),
            "lock_at": entry.get("lock_at") or ann.get("lock_at"),
            "n_samples": len(entry.get("samples") or []),
            "clv_points": round(compute_clv(open_s["line"], close_s["line"], ann["call"]), 2),
        })
    return plays, skipped_no_samples


def _plays_from_legacy(snapshot):
    """2026 file format: {player_id: {open_line, close_line, call, actionable_at_close, ...}}."""
    plays = []
    for pid, entry in snapshot.items():
        if not isinstance(entry, dict):
            continue
        if not entry.get("actionable_at_close") or entry.get("call") not in ("over", "under"):
            continue
        open_line, close_line = entry.get("open_line"), entry.get("close_line")
        if open_line is None or close_line is None:
            continue
        plays.append({
            "player_id": pid, "name": entry.get("name"), "team": entry.get("team"),
            "call": entry["call"], "open_line": open_line, "open_at": entry.get("open_at"),
            "close_line": close_line, "close_at": entry.get("close_at"),
            "n_samples": 1 if open_line == close_line else 2,  # best available reconstruction
            "clv_points": round(compute_clv(open_line, close_line, entry["call"]), 2),
        })
    return plays, 0


def _recompute_record(data):
    all_plays = [p for dd in data["dates"].values() for p in dd.get("plays", [])]
    positive = sum(1 for p in all_plays if p["clv_points"] > 0)
    negative = sum(1 for p in all_plays if p["clv_points"] < 0)
    push = sum(1 for p in all_plays if p["clv_points"] == 0)
    n = len(all_plays)
    multi = [p for p in all_plays if p.get("n_samples", 1) >= 2]
    data["record"] = {
        "positive": positive,
        "negative": negative,
        "push": push,
        "n": n,
        "win_rate": round(100 * positive / (positive + negative), 1) if (positive + negative) else None,
        "avg_clv_points": round(sum(p["clv_points"] for p in all_plays) / n, 3) if n else None,
        # v2 diagnostics - how much of the record rests on a real two-sample capture
        "plays_multi_sample": len(multi),
        "plays_single_sample": n - len(multi),
        "avg_clv_points_multi_sample": round(sum(p["clv_points"] for p in multi) / len(multi), 3) if multi else None,
        "skipped_no_samples": sum(dd.get("skipped_no_samples", 0) for dd in data["dates"].values()),
    }


def grade_clv(date_str, results_path=None):
    """Grade the day's forward CLV from its snapshot file (called nightly
    from tracker.py alongside the other grade_* functions). Only plays that
    were actionable at the last pre-lock render and had a real directional
    call count. Like every other results file in this codebase, `record`
    is fully recomputed from `dates` on every run, never accumulated
    incrementally. `results_path` overrides CLV_RESULTS_PATH (tests, and
    the clv_snapshot.py --grade verification path)."""
    results_path = results_path or CLV_RESULTS_PATH
    snapshot = load_snapshot(date_str)
    if snapshot is None:
        print(f"CLV: no snapshot for {date_str} (nothing captured that day?) - skipping.")
        return None

    if is_legacy_snapshot(snapshot):
        plays, skipped = _plays_from_legacy(snapshot)
    else:
        plays, skipped = _plays_from_v2(snapshot)

    data = {"dates": {}, "record": {}}
    if os.path.exists(results_path):
        try:
            with open(results_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    data.setdefault("dates", {})
    data["dates"][date_str] = {"plays": plays, "skipped_no_samples": skipped}
    _recompute_record(data)

    os.makedirs(os.path.dirname(results_path) or ".", exist_ok=True)
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    rec = data["record"]
    today_pos = sum(1 for p in plays if p["clv_points"] > 0)
    today_multi = sum(1 for p in plays if p.get("n_samples", 1) >= 2)
    print(f"CLV: {today_pos}/{len(plays)} positive today ({today_multi} with a real two-sample capture"
          f"{f', {skipped} skipped: no live samples' if skipped else ''}) -> running record "
          f"{rec['positive']}-{rec['negative']}-{rec['push']} push (n={rec['n']})"
          + (f", {rec['win_rate']}% positive, avg {rec['avg_clv_points']:+.3f} pts" if rec["n"] and rec["avg_clv_points"] is not None else ""))
    return data
