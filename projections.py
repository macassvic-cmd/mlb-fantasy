"""
MLB Fantasy Projections — reads daily JSON from the pipeline and prints
ranked leaderboards for both Underdog and PrizePicks scoring.

Usage:
  python projections.py              # most recent data file
  python projections.py 2025-06-05   # specific date
"""

import glob
import json
import os
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Scoring systems
# ---------------------------------------------------------------------------

UD_SCORING = {"1b": 3, "2b": 6, "3b": 8, "hr": 10, "bb": 3, "hbp": 3, "rbi": 2, "r": 2, "sb": 4}
PP_SCORING = {"1b": 3, "2b": 5, "3b": 8, "hr": 10, "bb": 2, "hbp": 2, "rbi": 2, "r": 2, "sb": 5}


def _score(proj, scoring):
    return round(
        proj.get("singles", 0) * scoring["1b"]
        + proj.get("doubles", 0) * scoring["2b"]
        + proj.get("triples", 0) * scoring["3b"]
        + proj.get("hr", 0) * scoring["hr"]
        + proj.get("bb", 0) * scoring["bb"]
        + proj.get("hbp", 0) * scoring["hbp"]
        + proj.get("rbi", 0) * scoring["rbi"]
        + proj.get("runs", 0) * scoring["r"]
        + proj.get("sb", 0) * scoring["sb"],
        2,
    )


def ud_fpts(player):
    proj = player.get("projected") or {}
    if proj:
        return _score(proj, UD_SCORING)
    return player.get("rolling_14d", {}).get("ud_fpts_per_game", 0) or 0


def pp_fpts(player):
    proj = player.get("projected") or {}
    if proj:
        return _score(proj, PP_SCORING)
    return player.get("rolling_14d", {}).get("pp_fpts_per_game", 0) or 0


# ---------------------------------------------------------------------------
# Composite ranking score
# ---------------------------------------------------------------------------

def composite_score(player, scoring="ud"):
    """
    Weighted blend of rolling form + matchup + weather + batting-order bonuses.
    Returns a float used purely for ranking.
    """
    key = "ud_fpts_per_game" if scoring == "ud" else "pp_fpts_per_game"
    r7  = (player.get("rolling_7d")  or {}).get(key, 0) or 0
    r14 = (player.get("rolling_14d") or {}).get(key, 0) or 0
    r30 = (player.get("rolling_30d") or {}).get(key, 0) or 0

    # Recency-weighted rolling average
    base = r7 * 0.40 + r14 * 0.35 + r30 * 0.25

    # Matchup bonus: easy ERA / low K% = more offense
    matchup = player.get("matchup", {})
    era = matchup.get("era") or matchup.get("era_fg") or 4.20
    k_pct = matchup.get("k_pct") or 0.23
    era = min(float(era), 9.99)
    matchup_bonus = (float(era) - 4.20) * 0.10 - (float(k_pct) - 0.23) * 2.0

    # Platoon advantage
    platoon = player.get("platoon", {})
    platoon_bonus = 0.20 if not platoon.get("platoon_same_hand", True) else -0.10

    # Weather bonus (outdoor only, hot / windy)
    wx = player.get("weather", {})
    wx_bonus = 0.0
    if not wx.get("is_indoor") and wx.get("weather_available"):
        if (wx.get("temp_f") or 72) > 82:
            wx_bonus += 0.15
        if (wx.get("wind_speed_mph") or 5) > 12:
            wx_bonus += 0.20

    # Batting order (top of order = more PAs)
    order = player.get("batting_order") or 5
    order_bonus = {1: 0.50, 2: 0.40, 3: 0.35, 4: 0.25, 5: 0.10}.get(order, 0.0)

    # Days rest (fresh = slight bonus; >3 days off = fatigue risk flag only, not penalized here)
    rest = player.get("days_rest")
    rest_bonus = 0.10 if rest == 1 else 0.0

    return round(base + matchup_bonus + platoon_bonus + wx_bonus + order_bonus + rest_bonus, 3)


# ---------------------------------------------------------------------------
# Leaderboard rendering
# ---------------------------------------------------------------------------

COL = {
    "rank":  5,
    "name":  22,
    "pos":   5,
    "bat":   4,
    "team":  14,
    "score": 7,
    "r7":    6,
    "r14":   6,
    "r30":   6,
    "era":   7,
    "woba":  6,
    "xwoba": 7,
    "park":  6,
    "wx":    14,
}


def _fmt(val, width, decimals=2, fallback="N/A"):
    if val is None:
        return fallback.ljust(width)
    return f"{val:.{decimals}f}".ljust(width)


def print_leaderboard(players, scoring="ud", top_n=30, date_str=""):
    label = "UNDERDOG" if scoring == "ud" else "PRIZEPICKS"
    key = "ud_fpts_per_game" if scoring == "ud" else "pp_fpts_per_game"
    fpts_fn = ud_fpts if scoring == "ud" else pp_fpts

    scored = []
    for p in players:
        try:
            scored.append({
                "_p": p,
                "comp": composite_score(p, scoring),
                "fpts": fpts_fn(p),
                "r7":   (p.get("rolling_7d")  or {}).get(key, 0) or 0,
                "r14":  (p.get("rolling_14d") or {}).get(key, 0) or 0,
                "r30":  (p.get("rolling_30d") or {}).get(key, 0) or 0,
            })
        except Exception:
            pass

    scored.sort(key=lambda x: x["comp"], reverse=True)

    W = 100
    print(f"\n{'=' * W}")
    print(f"  MLB FANTASY LEADERBOARD — {label}  |  {date_str}")
    print(f"{'=' * W}")
    hdr = (f"{'#':<5}{'Name':<22}{'Pos':<5}{'Bat':<4}{'Team':<14}"
           f"{'Score':<7}{'7d/g':<6}{'14d/g':<6}{'30d/g':<6}"
           f"{'OppERA':<7}{'wOBA':<6}{'xwOBA':<7}{'Park':<6}{'Weather':<14}")
    print(hdr)
    print("-" * W)

    for rank, row in enumerate(scored[:top_n], 1):
        p = row["_p"]
        matchup = p.get("matchup", {})
        fg     = p.get("fg_stats", {}) or {}
        sc     = p.get("statcast", {}) or {}
        wx     = p.get("weather", {}) or {}
        pf     = p.get("park_factor", {}) or {}

        era_val   = matchup.get("era") or matchup.get("era_fg")
        woba_val  = fg.get("woba")
        xwoba_val = sc.get("xwoba_14d") or sc.get("xwoba_30d")
        hr_pf     = pf.get("hr")

        temp = wx.get("temp_f")
        wind = wx.get("wind_speed_mph")
        wx_str = ""
        if temp is not None:
            wx_str = f"{int(temp)}°F"
        if wind is not None:
            wx_str += f" {int(wind)}mph {wx.get('wind_direction','')}"
        if wx.get("is_indoor"):
            wx_str = "Indoor"
        if not wx_str:
            wx_str = "N/A"

        order = p.get("batting_order")
        order_str = str(order) if order else "?"

        print(
            f"{rank:<5}{p.get('name',''):<22}{p.get('position',''):<5}"
            f"{p.get('bat_side',''):<4}{p.get('team_name',''):<14}"
            f"{row['comp']:<7.2f}{row['r7']:<6.1f}{row['r14']:<6.1f}{row['r30']:<6.1f}"
            f"{_fmt(era_val,7)}{_fmt(woba_val,6,3)}{_fmt(xwoba_val,7,3)}"
            f"{_fmt(hr_pf,6,2)}{wx_str:<14}"
        )

    print(f"\nScore = weighted rolling fpts/g  +  matchup  +  platoon  +  park/weather/order bonuses")


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def build_rows(players, scoring="ud"):
    """Return a list of flat dicts suitable for CSV export."""
    key = "ud_fpts_per_game" if scoring == "ud" else "pp_fpts_per_game"
    fpts_fn = ud_fpts if scoring == "ud" else pp_fpts
    rows = []
    for p in players:
        sc   = p.get("statcast", {}) or {}
        fg   = p.get("fg_stats", {}) or {}
        wx   = p.get("weather", {}) or {}
        pf   = p.get("park_factor", {}) or {}
        mt   = p.get("matchup", {}) or {}
        pl   = p.get("platoon", {}) or {}
        r7   = p.get("rolling_7d", {}) or {}
        r14  = p.get("rolling_14d", {}) or {}
        r30  = p.get("rolling_30d", {}) or {}
        rows.append({
            "name":              p.get("name", ""),
            "position":          p.get("position", ""),
            "bat_side":          p.get("bat_side", ""),
            "team":              p.get("team_name", ""),
            "home_away":         p.get("home_away", ""),
            "batting_order":     p.get("batting_order", ""),
            "venue":             p.get("venue_name", ""),
            "opp_team":          p.get("opp_team_name", ""),
            "opp_pitcher":       mt.get("pitcher_name", ""),
            "pitcher_hand":      mt.get("pitcher_hand", ""),
            "days_rest":         p.get("days_rest", ""),
            "lineup_confirmed":  p.get("lineup_confirmed", False),
            # composite score
            "composite_score":   round(composite_score(p, scoring), 3),
            # projected fpts
            "projected_fpts":    fpts_fn(p),
            # rolling fpts per game
            "rolling_7d_fpts_g":  r7.get(key, ""),
            "rolling_14d_fpts_g": r14.get(key, ""),
            "rolling_30d_fpts_g": r30.get(key, ""),
            # advanced stats
            "woba":              fg.get("woba", ""),
            "wrc_plus":          fg.get("wrc_plus", ""),
            "bb_pct":            fg.get("bb_pct", ""),
            "k_pct":             fg.get("k_pct", ""),
            "iso":               fg.get("iso", ""),
            "babip":             fg.get("babip", ""),
            "xwoba_7d":          sc.get("xwoba_7d", ""),
            "xwoba_14d":         sc.get("xwoba_14d", ""),
            "xwoba_30d":         sc.get("xwoba_30d", ""),
            "barrel_pct_7d":     sc.get("barrel_pct_7d", ""),
            "barrel_pct_14d":    sc.get("barrel_pct_14d", ""),
            "barrel_pct_30d":    sc.get("barrel_pct_30d", ""),
            "hard_hit_pct_7d":   sc.get("hard_hit_pct_7d", ""),
            "hard_hit_pct_14d":  sc.get("hard_hit_pct_14d", ""),
            "hard_hit_pct_30d":  sc.get("hard_hit_pct_30d", ""),
            "avg_ev_7d":         sc.get("avg_ev_7d", ""),
            "avg_ev_14d":        sc.get("avg_ev_14d", ""),
            "avg_ev_30d":        sc.get("avg_ev_30d", ""),
            "ev_stdev_30d":      sc.get("ev_stdev_30d", ""),
            "ev_floor_30d":      sc.get("ev_floor_30d", ""),
            "ev_ceiling_30d":    sc.get("ev_ceiling_30d", ""),
            "doc_avg_ev_30d":    sc.get("doc_avg_ev_30d", ""),
            # matchup
            "opp_era":           mt.get("era") or mt.get("era_fg", ""),
            "opp_fip":           mt.get("fip", ""),
            "opp_whip":          mt.get("whip") or mt.get("whip_fg", ""),
            "opp_k_pct":         mt.get("k_pct", ""),
            # platoon
            "platoon_same_hand": pl.get("platoon_same_hand", ""),
            "woba_vs_pitcher":   pl.get("woba_vs_pitcher", ""),
            # park
            "park_hr_factor":    pf.get("hr", ""),
            "park_runs_factor":  pf.get("runs", ""),
            "park_hits_factor":  pf.get("hits", ""),
            # weather
            "temp_f":            wx.get("temp_f", ""),
            "wind_speed_mph":    wx.get("wind_speed_mph", ""),
            "wind_direction":    wx.get("wind_direction", ""),
            "precip_pct":        wx.get("precip_probability", ""),
            "is_indoor":         wx.get("is_indoor", ""),
        })
    rows.sort(key=lambda r: r["composite_score"], reverse=True)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    # Put rank first
    ordered_keys = ["rank"] + [k for k in rows[0] if k != "rank"]
    return [{k: r[k] for k in ordered_keys} for r in rows]


def save_csv(players, csv_path, scoring="ud"):
    import csv
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    rows = build_rows(players, scoring)
    if not rows:
        print("No rows to write.")
        return
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved {len(rows)} rows -> {csv_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def load_data(date_str=None):
    if date_str is None:
        files = sorted(glob.glob(os.path.join("data", "*.json")))
        if not files:
            print("ERROR: No data files found in data/. Run pipeline.py first.")
            sys.exit(1)
        path = files[-1]
        date_str = os.path.splitext(os.path.basename(path))[0]
        print(f"Using most recent file: {path}")
    else:
        path = os.path.join("data", f"{date_str}.json")
        if not os.path.exists(path):
            print(f"ERROR: {path} not found. Run pipeline.py --date {date_str} first.")
            sys.exit(1)

    with open(path, encoding="utf-8") as f:
        players = json.load(f)

    return players, date_str


def main():
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    players, date_str = load_data(date_arg)
    total = len(players)
    confirmed = sum(1 for p in players if p.get("lineup_confirmed"))

    print(f"\nLoaded {total} players for {date_str}  ({confirmed} lineup-confirmed)")

    print_leaderboard(players, scoring="ud", date_str=date_str)
    print_leaderboard(players, scoring="pp", date_str=date_str)

    print(f"\n{'='*60}")
    print(f"Columns: Score=composite rank | Xd/g=rolling fpts per game")
    print(f"OppERA=facing pitcher ERA | Park=HR park factor (1.00=neutral)")
    print(f"Underdog scoring: 1B+3 2B+6 3B+8 HR+10 BB/HBP+3 RBI/R+2 SB+4")
    print(f"PrizePicks:       1B+3 2B+5 3B+8 HR+10 BB/HBP+2 RBI/R+2 SB+5")

    # CSV export — Underdog-ranked, all columns, opens cleanly in Excel
    csv_path = os.path.join("output", "projections_today.csv")
    save_csv(players, csv_path, scoring="ud")
    print(f"\nCSV saved to: {os.path.abspath(csv_path)}")


if __name__ == "__main__":
    main()
