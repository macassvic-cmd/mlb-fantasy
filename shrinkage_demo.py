"""One-shot before/after demo of the Bayesian shrinkage added 2026-09-13
(see shrinkage.py). Not part of the pipeline - run manually to eyeball that
the shrinkage direction/magnitude make sense, e.g. after tuning
SHRINKAGE_PRIOR_STRENGTH.

Usage:
  python shrinkage_demo.py
"""

import json
import os

from report import top25_pooled_baseline
from shrinkage import shrink_rate, pooled_rate

TOP25_RESULTS_PATH = os.path.join("data", "results", "top25_results.json")
UD_UNDER_BAND_RESULTS_PATH = os.path.join("data", "results", "ud_under_band_results.json")


def _load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def show_top25():
    data = _load(TOP25_RESULTS_PATH)
    players = data.get("players", {})
    if not players:
        print("No top25_results.json data found.")
        return
    baseline = top25_pooled_baseline(players)
    print(f"\n=== Top-25 record: raw vs. shrunk (baseline={baseline*100:.1f}%) ===")
    print(f"{'Player':<24}{'W-L':<10}{'n':<5}{'raw%':<8}{'shrunk%':<8}")
    rows = []
    for p in players.values():
        history = p.get("history", [])
        wins = sum(1 for h in history if h.get("grade") == "win")
        losses = sum(1 for h in history if h.get("grade") == "loss")
        n = wins + losses
        if n == 0:
            continue
        raw = round(100 * wins / n, 1)
        shrunk = round(100 * shrink_rate(wins, losses, baseline), 1)
        rows.append((p.get("name", "?"), wins, losses, n, raw, shrunk))
    rows.sort(key=lambda r: r[3])  # ascending n - smallest-sample effects first
    for name, w, l, n, raw, shrunk in rows:
        print(f"{name:<24}{f'{w}-{l}':<10}{n:<5}{raw:<8}{shrunk:<8}")


def show_under_band():
    data = _load(UD_UNDER_BAND_RESULTS_PATH)
    by_player = data.get("record_by_player", {})
    if not by_player:
        print("\nNo record_by_player yet in ud_under_band_results.json "
              "(populated by tracker.grade_ud_under_band after this update ships).")
        return
    total_wins = sum(p.get("wins", 0) for p in by_player.values())
    total_losses = sum(p.get("losses", 0) for p in by_player.values())
    baseline = pooled_rate(total_wins, total_losses)
    print(f"\n=== UNDER band record by player: raw vs. shrunk (baseline={baseline*100:.1f}%) ===")
    print(f"{'Player':<24}{'W-L':<10}{'n':<5}{'raw%':<8}{'shrunk%':<8}")
    rows = []
    for pid, p in by_player.items():
        w, l = p.get("wins", 0), p.get("losses", 0)
        n = w + l
        if n == 0:
            continue
        raw = round(100 * w / n, 1)
        shrunk = round(100 * shrink_rate(w, l, baseline), 1)
        rows.append((p.get("name", "?"), w, l, n, raw, shrunk))
    rows.sort(key=lambda r: r[3])
    for name, w, l, n, raw, shrunk in rows:
        print(f"{name:<24}{f'{w}-{l}':<10}{n:<5}{raw:<8}{shrunk:<8}")


if __name__ == "__main__":
    show_top25()
    show_under_band()
