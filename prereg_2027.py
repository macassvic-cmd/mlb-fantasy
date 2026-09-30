"""Pre-registered 2027 hypotheses - the executable form of PREREG_2027.md.

Read-only. Evaluates both locked rules against whatever graded data is on
disk and prints record, Wilson CI, and PASS / FAIL / INCONCLUSIVE against
the breakevens and sample floors written into PREREG_2027.md on
2026-09-29. The rules below are frozen with that document: if this file
and that document ever disagree, the document wins and this file is the
bug.

Usage:
  python prereg_2027.py                    # evaluate on all graded dates
  python prereg_2027.py --season 2027      # restrict to one season's dates
  python prereg_2027.py --thresholds       # print the observed-rate bar at various n

Both rules are subsets of cohorts the pipeline already persists BEFORE
games start (data/ud_under_band_plays, data/results/top25_results.json's
per-date entries), so membership can't be chosen after the fact.
"""

import argparse
import json
import math
import os
import sys

from report import wilson_ci, LOW_COVERAGE_DATES, FIRE_MIN_N, MARK_MIN_RATE, TOP25_OVER_EDGE_MIN
from shrinkage import shrink_rate, pooled_rate, SHRINKAGE_PRIOR_STRENGTH

RESULTS_DIR = os.path.join("data", "results")

# ---- locked parameters (mirror PREREG_2027.md exactly) ---------------------
BREAKEVEN_PRIMARY = 0.55     # pick'em 3-pick at 6x
BREAKEVEN_SECONDARY = 0.577  # pick'em 2-pick at 3x
FAMILY_ALPHA = 0.05
N_HYPOTHESES = 2
ALPHA_ONE_SIDED = FAMILY_ALPHA / N_HYPOTHESES   # 0.025 per hypothesis (Bonferroni)
Z = 1.96                                        # one-sided 0.025 == two-sided 0.05 Wilson bound

H1_MIN_LINE = 7.5
H1_EDGE_LO, H1_EDGE_HI = -2.0, -1.5             # edge in (-2.0, -1.5]  (report.in_ud_under_band)
H1_MIN_N = 150

H2_MIN_N_BEFORE = FIRE_MIN_N                    # 15 prior Top-25 decisions
H2_MIN_SHRUNK = MARK_MIN_RATE                   # 0.55 shrunk rate (prior strength 20)
H2_MIN_EDGE = TOP25_OVER_EDGE_MIN               # +1.0 line points, OVER only
H2_MIN_N = 120
H2_CARRY_2026_HISTORY = True                    # 2026 Top-25 history seeds the 2027 record


def _load(name, default):
    p = os.path.join(RESULTS_DIR, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _in_season(date_str, season):
    return season is None or date_str.startswith(f"{season}-")


def verdict(wins, losses, min_n, breakeven=BREAKEVEN_PRIMARY):
    n = wins + losses
    if n < min_n:
        return "INCONCLUSIVE (below sample floor)"
    lo, _ = wilson_ci(wins, n, z=Z)
    if lo / 100 > breakeven:
        return "PASS"
    return "FAIL"


def fmt(wins, losses):
    n = wins + losses
    if not n:
        return "0-0 (n=0)"
    lo, hi = wilson_ci(wins, n, z=Z)
    return f"{wins}-{losses} ({100 * wins / n:.1f}%, n={n}, 95% Wilson {lo}-{hi}%)"


# ---- H1: high-line UNDER slice ---------------------------------------------

def h1_plays(season=None):
    """Every graded UNDER-band play with ud_line >= 7.5. Membership is
    exactly report.in_ud_under_band (edge in (-2.0, -1.5], market-anchored)
    plus the line floor; excludes DNP/ungraded and LOW_COVERAGE_DATES."""
    data = _load("ud_under_band_results.json", {"dates": {}})
    out = []
    for d, dd in sorted(data.get("dates", {}).items()):
        if d in LOW_COVERAGE_DATES or not _in_season(d, season):
            continue
        for p in dd.get("plays", []):
            line, edge = p.get("ud_line"), p.get("edge")
            if line is None or edge is None or line < H1_MIN_LINE:
                continue
            if not (H1_EDGE_LO < edge <= H1_EDGE_HI):
                continue
            if p.get("grade") in ("win", "loss"):
                out.append(dict(p, date=d))
    return out


# ---- H2: track-record filter -----------------------------------------------

def h2_plays(season=None):
    """Top-25 OVERs where, using ONLY history strictly before that date:
    the player has >= 15 graded Top-25 decisions, a shrunk rate >= 0.55
    (Beta-Binomial, prior strength 20, baseline = pooled Top-25 rate before
    that date), AND the day's market edge (projected_ud - ud_line) >= +1.0.
    Point-in-time by construction, mirroring tracker.track_marked_plays."""
    top25 = _load("top25_results.json", {"dates": {}, "players": {}})
    histories = {pid: sorted(p.get("history", []), key=lambda h: h["date"])
                 for pid, p in top25.get("players", {}).items()}
    out = []
    for d in sorted(top25.get("dates", {})):
        if not _in_season(d, season):
            continue
        pool_w = pool_l = 0
        for hl in histories.values():
            for h in hl:
                if h["date"] < d:
                    if h.get("grade") == "win":
                        pool_w += 1
                    elif h.get("grade") == "loss":
                        pool_l += 1
        baseline = pooled_rate(pool_w, pool_l)
        for e in top25["dates"][d].get("top25", []):
            proj, line = e.get("projected_ud", e.get("ud")), e.get("ud_line")
            if proj is None or line is None:
                continue
            edge = proj - line
            if edge < H2_MIN_EDGE:
                continue
            before = [h for h in histories.get(str(e["player_id"]), []) if h["date"] < d]
            w = sum(1 for h in before if h.get("grade") == "win")
            n = w + sum(1 for h in before if h.get("grade") == "loss")
            if n < H2_MIN_N_BEFORE:
                continue
            shrunk = shrink_rate(w, n - w, baseline, SHRINKAGE_PRIOR_STRENGTH)
            if shrunk < H2_MIN_SHRUNK:
                continue
            if e.get("grade") in ("win", "loss"):
                out.append({"date": d, "player_id": e["player_id"], "name": e.get("name"), "edge": round(edge, 2),
                            "n_before": n, "wins_before": w, "shrunk_before": round(shrunk, 3),
                            "grade": e["grade"]})
    return out


# ---- thresholds ------------------------------------------------------------

def required_rate(n, breakeven, z=Z):
    """Smallest observed win rate whose Wilson lower bound clears breakeven at sample n."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        w = mid * n
        if wilson_ci(w, n, z=z)[0] / 100 > breakeven:
            hi = mid
        else:
            lo = mid
    return hi


def print_thresholds():
    print(f"Observed rate needed for the Wilson lower bound (z={Z}, one-sided alpha={ALPHA_ONE_SIDED}) to clear breakeven:")
    print(f"  {'n':>5}  {'>55.0%':>8}  {'>57.7%':>8}")
    for n in (100, 150, 200, 250, 300, 400, 500):
        print(f"  {n:>5}  {100 * required_rate(n, BREAKEVEN_PRIMARY):>7.1f}%  {100 * required_rate(n, BREAKEVEN_SECONDARY):>7.1f}%")


# ---- main ------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--thresholds", action="store_true")
    args = ap.parse_args(argv)
    if args.thresholds:
        print_thresholds()
        return 0

    label = f"season {args.season}" if args.season else "all graded dates"
    print(f"PREREG 2027 - evaluation on {label}")
    print(f"Bar: Wilson lower bound (z={Z}) > breakeven; primary breakeven {BREAKEVEN_PRIMARY:.1%}, "
          f"secondary {BREAKEVEN_SECONDARY:.1%}; Bonferroni alpha {ALPHA_ONE_SIDED} one-sided per hypothesis.\n")

    p1 = h1_plays(args.season)
    w = sum(1 for p in p1 if p["grade"] == "win"); l = len(p1) - w
    print(f"H1  High-line UNDER slice (ud_line >= {H1_MIN_LINE}, edge in ({H1_EDGE_LO}, {H1_EDGE_HI}], floor n={H1_MIN_N})")
    print(f"    {fmt(w, l)}")
    print(f"    vs 55.0%: {verdict(w, l, H1_MIN_N)}   vs 57.7%: {verdict(w, l, H1_MIN_N, BREAKEVEN_SECONDARY)}")

    p2 = h2_plays(args.season)
    w = sum(1 for p in p2 if p["grade"] == "win"); l = len(p2) - w
    print(f"\nH2  Track-record filter (n_before >= {H2_MIN_N_BEFORE}, shrunk >= {H2_MIN_SHRUNK}, OVER edge >= +{H2_MIN_EDGE}, floor n={H2_MIN_N})")
    print(f"    {fmt(w, l)}")
    print(f"    vs 55.0%: {verdict(w, l, H2_MIN_N)}   vs 57.7%: {verdict(w, l, H2_MIN_N, BREAKEVEN_SECONDARY)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
