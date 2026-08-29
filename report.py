"""
MLB Fantasy Report Generator
Builds an HTML dashboard (output/dashboard.html) from the most recent
pipeline data and deploys it to GitHub Pages.

Usage:
  python report.py              # most recent data file
  python report.py 2026-06-11   # specific date
"""

import json
import os
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import projections as proj
from scrapers.market_lines import get_market_lines, compute_pp_ud_ratio, match_lines
from scrapers.betr import get_betr_lines


# ---------------------------------------------------------------------------
# Derived fields
# ---------------------------------------------------------------------------

def confidence_score(p):
    """0-100 score reflecting how much data backs this projection."""
    score = 0
    r14 = p.get("rolling_14d") or {}
    sc = p.get("statcast") or {}
    fg = p.get("fg_stats") or {}
    wx = p.get("weather") or {}

    if p.get("lineup_confirmed"):
        score += 30

    g14 = r14.get("games", 0) or 0
    if g14 >= 10:
        score += 20
    elif g14 >= 5:
        score += 10

    if sc.get("xwoba_14d") is not None:
        score += 20

    if fg.get("woba") is not None:
        score += 10

    if wx.get("weather_available"):
        score += 10

    if p.get("days_rest") is not None:
        score += 10

    return min(score, 100)


def platoon_edge(p):
    """Yes/No/N/A based on the real wOBA-vs-wOBA-against matchup, not just
    handedness - falls back to N/A when either side's split sample is too
    small to trust (see fangraphs.match_platoon_matchup)."""
    pl = p.get("platoon") or {}
    adv = pl.get("advantage")
    if adv is None:
        return "N/A"
    return "Yes" if adv == "batter" else "No"


def weather_icon(row):
    if row.get("wx_indoor"):
        return "🏟️"
    precip = row.get("wx_precip")
    wind = row.get("wx_wind")
    temp = row.get("wx_temp")
    if precip is not None and precip >= 40:
        return "🌧️"
    if wind is not None and wind >= 12:
        return "💨"
    if temp is not None and temp >= 85:
        return "☀️"
    return "⛅"


def card_tier(ud_pts):
    if ud_pts >= 8:
        return "green"
    if ud_pts >= 5:
        return "yellow"
    return "red"


_PACIFIC = ZoneInfo("America/Los_Angeles")


def game_time_pt(game_date_utc):
    """Format an MLB API 'gameDate' UTC ISO timestamp as e.g. '7:05 PM PT'.
    Returns None if missing/unparseable so callers can skip it cleanly."""
    if not game_date_utc:
        return None
    try:
        dt_utc = datetime.fromisoformat(game_date_utc.replace("Z", "+00:00"))
        dt_pt = dt_utc.astimezone(_PACIFIC)
        return dt_pt.strftime("%-I:%M %p PT") if os.name != "nt" else dt_pt.strftime("%#I:%M %p PT")
    except (ValueError, TypeError):
        return None


def _is_early_game(game_date_utc):
    """Returns True if the game starts before 18:00 UTC (= 11:00 AM PT in
    summer), the window where getaway-day morning games cluster."""
    if not game_date_utc:
        return False
    try:
        dt = datetime.fromisoformat(game_date_utc.replace("Z", "+00:00"))
        return dt.hour < 18
    except (ValueError, TypeError):
        return False


def edge_label(edge):
    """Classify our projection vs. the posted UD line into OVER/UNDER/NEUTRAL."""
    if edge is None:
        return None
    if edge > 0.5:
        return "over"
    if edge < -0.5:
        return "under"
    return "neutral"


TOP25_RECORD_MIN_N_FOR_RATE = 8


def top25_record_badge(player_id, top25_players):
    """Cumulative Top-25 appearance record for this player, from
    top25_results.json's per-player history (win/loss vs the UD line,
    excluding pushes). Always the current running total regardless of
    which date's card is showing it - not a point-in-time snapshot."""
    p = top25_players.get(str(player_id)) if player_id is not None else None
    history = p.get("history", []) if p else []
    wins = sum(1 for h in history if h.get("grade") == "win")
    losses = sum(1 for h in history if h.get("grade") == "loss")
    n = wins + losses
    if n == 0:
        text = "Top 25: first appearance"
    elif n < TOP25_RECORD_MIN_N_FOR_RATE:
        text = f"Top 25: {wins}-{losses} (n={n})"
    else:
        rate = round(100 * wins / n, 1)
        text = f"Top 25: {wins}-{losses} ({rate}%)"
    return {"text": text, "wins": wins, "losses": losses, "n": n}


def build_card(row):
    return {
        "playerId": row.get("player_id"),
        "name":   row["name"],
        "team":   row["team"],
        "order":  row["order"] or "-",
        "ud":     fmt_value(row["ud_pts"], "1f"),
        "pp":     fmt_value(row["pp_pts"], "1f"),
        "xwoba":  fmt_value(row["xwoba"], "3f"),
        "barrel": fmt_value(row["barrel_pct"], "1f"),
        "era":    fmt_value(row["opp_era"], "2f"),
        "wxIcon": weather_icon(row),
        "wxText": row["weather"],
        "park":   fmt_value(row["park_hr"], "2f"),
        "platoon": row["platoon_edge"] == "Yes",
        "platoonMatchup": {
            "batterWoba":    row.get("platoon_batter_woba"),
            "batterLabel":   row.get("platoon_batter_label"),
            "pitcherWoba":   row.get("platoon_pitcher_woba"),
            "pitcherLabel":  row.get("platoon_pitcher_label"),
            "advantage":     row.get("platoon_advantage"),
        } if row.get("platoon_advantage") else None,
        "adjusted": row.get("adjusted", False),
        "anchored": row.get("market_anchored", False),
        "noLinePenalty": row.get("no_line_penalty", False),
        "getawayDayRisk": row.get("getaway_day_risk", False),
        "projectedLineup": row.get("lineup_status") == "projected",
        "tier":   card_tier(row["ud_pts"]),
        "edge":   row.get("edge"),
        "udUnderBand": in_ud_under_band(row),
        "udLine": row.get("ud_line"),
        "edgeLabel": edge_label(row.get("edge")),
        "gameTimePt":  row.get("game_time_pt"),
        "gameDateUtc": row.get("game_date_utc"),
    }


def weather_str(p):
    wx = p.get("weather") or {}
    if wx.get("is_indoor"):
        return "Indoor"
    if not wx.get("weather_available"):
        return "N/A"
    temp = wx.get("temp_f")
    wind = wx.get("wind_speed_mph")
    wd = wx.get("wind_direction", "") or ""
    parts = []
    if temp is not None:
        parts.append(f"{temp:.0f}°F")
    if wind is not None:
        parts.append(f"{wind:.0f}mph {wd}".strip())
    return " ".join(parts) if parts else "N/A"


def build_row(p):
    sc = p.get("statcast") or {}
    fg = p.get("fg_stats") or {}
    mt = p.get("matchup") or {}
    pf = p.get("park_factor") or {}
    wx = p.get("weather") or {}
    r7 = p.get("rolling_7d") or {}
    r14 = p.get("rolling_14d") or {}

    return {
        "player_id":    p.get("player_id"),
        "name":         p.get("name", ""),
        "team":         p.get("team_name", ""),
        "opp_team":     p.get("opp_team_name", ""),
        "pos":          p.get("position", ""),
        "order":        p.get("batting_order") or 0,
        "ud_pts":       proj.ud_fpts(p),
        "pp_pts":       proj.pp_fpts(p),
        "confidence":   confidence_score(p),
        "xwoba":        sc.get("xwoba_14d") if sc.get("xwoba_14d") is not None else sc.get("xwoba_30d"),
        "barrel_pct":   sc.get("barrel_pct_14d"),
        "hard_hit_pct": sc.get("hard_hit_pct_14d"),
        "ev":           sc.get("avg_ev_14d"),
        "r7":           r7.get("ud_fpts_per_game"),
        "r14":          r14.get("ud_fpts_per_game"),
        "opp_sp":       mt.get("pitcher_name", ""),
        "opp_era":      mt.get("era") if mt.get("era") is not None else mt.get("era_fg"),
        "opp_fip":      mt.get("fip"),
        "weather":      weather_str(p),
        "wx_indoor":    bool(wx.get("is_indoor")),
        "wx_temp":      wx.get("temp_f"),
        "wx_wind":      wx.get("wind_speed_mph"),
        "wx_dir":       wx.get("wind_direction"),
        "wx_precip":    wx.get("precip_probability"),
        "park_hr":      pf.get("hr"),
        "days_rest":    p.get("days_rest"),
        "platoon_edge": platoon_edge(p),
        "platoon_batter_woba":    (p.get("platoon") or {}).get("batter_woba"),
        "platoon_batter_label":   (p.get("platoon") or {}).get("batter_split_label"),
        "platoon_pitcher_woba":   (p.get("platoon") or {}).get("pitcher_woba_against"),
        "platoon_pitcher_label":  (p.get("platoon") or {}).get("pitcher_split_label"),
        "platoon_advantage":      (p.get("platoon") or {}).get("advantage"),
        "comp":         proj.composite_score(p, "ud"),
        "game_pk":      p.get("game_pk"),
        "home_away":    p.get("home_away"),
        "venue":        p.get("venue_name", ""),
        "lineup_status":    p.get("lineup_status", "confirmed"),
        "lineup_confirmed": bool(p.get("lineup_confirmed", True)),
        "game_date_utc":    p.get("game_date_utc"),
        "game_time_pt":     game_time_pt(p.get("game_date_utc")),
    }


def tier(value, thresholds):
    """thresholds = (green_min, yellow_min)"""
    green_min, yellow_min = thresholds
    if value >= green_min:
        return "green"
    if value >= yellow_min:
        return "yellow"
    return "red"


def percentile(values, pct):
    s = sorted(values)
    if not s:
        return 0
    idx = int(round((pct / 100.0) * (len(s) - 1)))
    return s[idx]


# ---------------------------------------------------------------------------
# Projection recalibration
#
# pipeline.py's raw "projected" stat lines run hot (it scales whole rolling
# windows rather than realistic per-game rates), which inflates UD/PP points
# well past real sportsbook lines (which sit under ~10-12 even for elite
# hitters). Rather than re-deriving the model, we map each player's raw UD
# points to a realistic target band based on their percentile rank for the
# day, and carry PP points along at the same raw PP/UD ratio.
# ---------------------------------------------------------------------------

# (percentile, target UD pts) anchor points -> tuned to match the real
# UD/PP "Fantasy Points" line distribution (roughly 3.5-9.5 for a typical
# slate), so raw projections land close to market lines before anchoring.
PTS_CURVE = [
    (0.05, 3.5),
    (0.25, 4.5),
    (0.50, 6.0),
    (0.75, 7.5),
    (0.95, 9.5),
]


def pts_target(pctile):
    for (p0, v0), (p1, v1) in zip(PTS_CURVE, PTS_CURVE[1:]):
        if pctile <= p1:
            frac = (pctile - p0) / (p1 - p0) if p1 > p0 else 0
            return v0 + frac * (v1 - v0)
    return PTS_CURVE[-1][1]


def recalibrate_points(rows):
    """Rescale row['ud_pts'] / row['pp_pts'] in place to realistic ranges."""
    n = len(rows)
    order = sorted(range(n), key=lambda i: rows[i]["ud_pts"])
    for rank, i in enumerate(order):
        pctile = rank / (n - 1) if n > 1 else 1.0
        target_ud = pts_target(pctile)
        raw_ud = rows[i]["ud_pts"]
        raw_pp = rows[i]["pp_pts"]
        ratio = raw_pp / raw_ud if raw_ud > 0.01 else 0.93
        rows[i]["ud_pts"] = round(target_ud, 2)
        rows[i]["pp_pts"] = round(target_ud * ratio, 2)


# ---------------------------------------------------------------------------
# Market line anchoring
#
# Underdog and PrizePicks post their own "Fantasy Points" lines for today's
# games. Those lines are the real benchmark - our projection should land
# close to them, not float off on its own percentile-based scale. For any
# player with a posted line, we keep our raw recalibrated value only as an
# "edge" (clamped) on top of that line. The clamp must stay above the
# Value Plays threshold (1.5 pts, see VALUE_PLAY_EDGE below) or no edge can
# ever qualify as a Value Play.
# ---------------------------------------------------------------------------

MARKET_EDGE_CLAMP = 2.0
# Bucket rates below are a point-in-time snapshot (see slips.py's
# refresh_edge_bucket_rates() for the live, self-updating numbers these
# thresholds are based on). Last refreshed 2026-07-13 from the clean
# 6/14+ dataset (post 6/10-6/13 tracker-bug exclusion):
#   OVER  1.0-1.5 pt: 54.3% win — the single best OVER bucket
#   OVER  1.5-2.0 pt: 49.0% win — weak; raises the bar
#   UNDER 1.5-2.0 pt: 59.4% win — the single best UNDER bucket
#   UNDER 2.0+  pt:   52.6% win — max clamp is good but not best
#
# OVER threshold stays at 1.0 to capture the best OVER bucket without
# raising noise (0.5-1.0 pt OVERs lose at 49.5%, so they won't displace
# top-edge plays). UNDER stays at 1.5 — the 1.5-2.0 range is the sweet
# spot, not 2.0+.
VALUE_PLAY_OVER_EDGE  = 1.0   # OVER  value plays: 1.0 pt minimum (1.0-1.5 wins 54.3%)
VALUE_PLAY_UNDER_EDGE = 1.5   # UNDER value plays: 1.5 pt minimum (1.5-2.0 wins 59.4%)
VALUE_PLAY_EDGE = VALUE_PLAY_OVER_EDGE  # legacy alias
assert VALUE_PLAY_OVER_EDGE  < MARKET_EDGE_CLAMP, "OVER threshold must be below the market edge clamp"
assert VALUE_PLAY_UNDER_EDGE <= MARKET_EDGE_CLAMP, "UNDER threshold must not exceed the market edge clamp"

# ---------------------------------------------------------------------------
# UD UNDER 1.5-2.0 edge band - the one finding from the 2026-08-18 OVER vs
# UNDER analysis that replicated in AND out of sample: 57.8% (327-239,
# n=566) on UD alone, Wilson 95% CI floor 53.7%, split evenly between the
# discovery window (56.4%, n=374) and clean out-of-sample (56.7%, n=275).
# Deliberately just the raw bucket - UNDER call, edge in [1.5, 2.0), UD
# only, nothing else layered on top. Premium Tier A was this exact effect
# with a batting-order-3-4 filter mined on top, and that filter is what
# didn't survive out-of-sample (see slips.py) - so this stays unfiltered on
# purpose; do not add order/team/venue conditions to it. Tracked forward
# from UD_UNDER_BAND_SEED_END (data/results/ud_under_band_results.json,
# graded nightly by tracker.grade_ud_under_band), separate from every
# retired tier, to keep testing whether it holds past 566 plays.
# ---------------------------------------------------------------------------
UD_UNDER_BAND_LO = 1.5
UD_UNDER_BAND_HI = 2.0  # exclusive - the exact-2.0 clamp bucket performs differently (51.0%) and is NOT included
UD_UNDER_BAND_SEED = {"wins": 327, "losses": 239, "n": 566}
UD_UNDER_BAND_SEED_END = "2026-08-17"  # tracker.grade_ud_under_band only counts dates after this
UD_UNDER_BAND_RESULTS_PATH = os.path.join("data", "results", "ud_under_band_results.json")
MARKED_PLAYS_RESULTS_PATH = os.path.join("data", "results", "marked_plays_results.json")

# Dates whose UD line coverage was too thin for that day's grading to be
# representative of anything - see scrapers/market_lines.py's re-fetch-if-
# thin fix (2026-08-29), added after an unlucky early-UTC first-fetch each
# day locked in a near-empty board with no re-fetch. Single source of
# truth for both: (1) tracker.grade_ud_under_band, which excludes these
# dates from the UD UNDER 1.5-2.0 band's cumulative record/rate (a
# 15-33-line board can't produce a representative band sample), and
# (2) the Results/Under Results calendar cells, which mark these dates
# visibly caveated rather than silently blending them in with normal days.
# Historical only - not something new dates get added to automatically;
# add an entry here if another day is later found to have the same issue.
LOW_COVERAGE_DATES = {
    "2026-08-27": "UD line coverage ~12% (15/126 players)",
    "2026-08-28": "UD line coverage ~3.7% (10/270 players)",
}


def in_ud_under_band(row):
    edge = row.get("edge")
    return bool(row.get("market_anchored")) and edge is not None and -UD_UNDER_BAND_HI < edge <= -UD_UNDER_BAND_LO


def _ud_under_band_combined_record():
    """(wins, losses, live_n) combining the frozen seed with whatever
    tracker.grade_ud_under_band has accumulated since UD_UNDER_BAND_SEED_END."""
    live = {"wins": 0, "losses": 0}
    if os.path.exists(UD_UNDER_BAND_RESULTS_PATH):
        try:
            with open(UD_UNDER_BAND_RESULTS_PATH, encoding="utf-8") as f:
                live = json.load(f).get("record", live)
        except Exception:
            pass
    wins = UD_UNDER_BAND_SEED["wins"] + live.get("wins", 0)
    losses = UD_UNDER_BAND_SEED["losses"] + live.get("losses", 0)
    live_n = live.get("wins", 0) + live.get("losses", 0)
    return wins, losses, live_n


def ud_under_band_label():
    """Combined seed + live-tracked record/rate, for the honest badge text
    ('57.8% (n=612)'), not a tier name."""
    wins, losses, live_n = _ud_under_band_combined_record()
    n = wins + losses
    rate = round(100 * wins / n, 1) if n else 0.0
    since_note = f"{live_n} tracked since {UD_UNDER_BAND_SEED_END}" if live_n else f"tracking since {UD_UNDER_BAND_SEED_END}"
    return f"UNDER 1.5–2.0 edge · {rate}% (n={n}, {since_note})"


def ud_under_band_header():
    """Header line for the Unders tab: 'UNDER 1.5-2.0 edge band: X-Y (Z%)
    tracked since 2026-08-17'."""
    wins, losses, _ = _ud_under_band_combined_record()
    n = wins + losses
    rate = round(100 * wins / n, 1) if n else 0.0
    return f"UNDER 1.5-2.0 edge band: {wins}-{losses} ({rate}%) tracked since {UD_UNDER_BAND_SEED_END}"


def wilson_ci(wins, n, z=1.96):
    """Wilson score interval (95% by default) for a win rate, as (lo, hi)
    percentages. More reliable than a normal-approximation CI at the
    sample sizes involved here."""
    if not n:
        return (0.0, 0.0)
    phat = wins / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * ((phat * (1 - phat) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (round(max(0, (center - margin) * 100), 1), round(min(100, (center + margin) * 100), 1))


UD_UNDER_BAND_PLAYS_DIR = os.path.join("data", "ud_under_band_plays")


def save_ud_under_band_plays(date_str, unders_rows):
    """Snapshot of today's validated-band UNDER calls (unders_rows is
    already filtered to the band - see write_dashboard), saved so
    tracker.grade_ud_under_band can grade them the next day even for
    players who end up DNPing. DNP'd players never get a
    data/results/all_results.json history entry at all (pipeline.py skips
    them outright - "didn't play (scratched, postponed, bench, etc.)"),
    so without this snapshot there'd be no way to tell "wasn't in the
    band" apart from "was in the band but didn't play"."""
    plays = [{
        "player_id": r.get("player_id"),
        "name": r["name"],
        "team": r["team"],
        "projected_ud": r.get("ud_pts"),
        "ud_line": r.get("ud_line"),
        "edge": r.get("edge"),
        "game_time_pt": r.get("game_time_pt"),
    } for r in unders_rows]
    os.makedirs(UD_UNDER_BAND_PLAYS_DIR, exist_ok=True)
    with open(os.path.join(UD_UNDER_BAND_PLAYS_DIR, f"{date_str}.json"), "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "plays": plays}, f, indent=2)


# Players without a posted UD/PP line are systematically more volatile than
# the market suggests — UD withholds lines when lineup status is uncertain.
# Discount their projection to reflect this hidden risk and make it harder
# for them to crack the Top 25 on model strength alone.
NO_LINE_PENALTY = 0.20

# Applied on top of NO_LINE_PENALTY when BOTH lineup_confirmed=False AND the
# game is an early start (before 18:00 UTC / 11 AM PT). These two signals
# compound each other: the market's silence + an unconfirmed lineup on a
# getaway-day morning game is the highest-risk profile we've identified.
# Combined total: ×0.80 × ×0.75 = ×0.60 (40% reduction from base).
GETAWAY_DAY_PENALTY = 0.25

# Venues where the model's raw projections have systematically over-estimated
# performance relative to the market line. Applied as a fractional reduction
# to ud_pts/pp_pts BEFORE market anchoring, compressing the edge at those
# parks so the model calls fewer high-confidence OVERs where it has
# historically been wrong.
#
# 2026-08-18 full recalibration, re-run after discovering the local repo was
# 13 dates stale (61 dates, 2026-06-14 to 2026-08-17, 8,193 graded plays;
# baseline 50.7%). Numbers moved again from the first (48-date) pass -
# Rogers Centre fell below the 3pt/n150 keep bar (-3.4% -> -2.2%, dropped),
# Kauffman/Tropicana/Colorado/Tampa Bay also fell below bar on the slips.py
# side (see there). T-Mobile Park flipped the other way: was ~flat on 48
# dates (-0.4%, dropped that pass) but full data shows a real -4.0% deficit
# (n=199) - added. PNC Park/Comerica Park/Fenway Park/Milwaukee Brewers stay
# dropped (confirmed noise both passes). This volatility at n~200-300 is
# itself the headline finding - see the recalibration report.
#   Oracle Park:    45.8% win rate (n=308), -5.0% vs 50.7% baseline
#   Citi Field:     46.6% win rate (n=268), -4.1%
#   Chase Field:    47.5% win rate (n=305), -3.2%
#   T-Mobile Park:  46.7% win rate (n=199), -4.0%
VENUE_PROJECTION_PENALTIES = {
    "Oracle Park":    0.04,
    "Citi Field":     0.03,
    "Chase Field":    0.02,
    "T-Mobile Park":  0.03,
}

# Teams with systematic model underperformance not fully explained by their home
# venue (applies to ALL games, home and away). Giants are already penalized at
# Oracle Park; only teams without a matching home-venue penalty are listed here.
#
# 2026-08-18 full recalibration (61 dates - see venue comment above):
# Washington Nationals evaporated further (-4.0% -> -0.6%, n=269) and is
# dropped - it looked like the one stable holdout on the first pass but
# didn't survive a second round of data. Seattle Mariners is new: was
# indistinguishable from noise on the partial sample (+0.2%) but full data
# shows a real -5.5% deficit (n=254) - the opposite direction of concern
# from what the name in this dict used to represent, worth double-checking
# again at the next recalibration before trusting it fully.
#   Seattle Mariners: 45.3% win rate (n=254), -5.5% vs 50.7% baseline
TEAM_PROJECTION_PENALTIES = {
    "Seattle Mariners": 0.04,
}


def apply_market_anchor(rows, market_lines, market_corrections=None):
    """Anchor row['ud_pts'] / row['pp_pts'] to today's posted UD/PP lines
    where available. If a player has a market_correction factor (how much
    they've historically beaten/missed their own line), that factor is
    applied to the line itself before adding the signal-based edge. Returns
    the set of player_ids that were anchored (these should be skipped by
    apply_corrections to avoid double adjustment)."""
    market_corrections = market_corrections or {}
    anchored = set()
    pp_ud_ratio = compute_pp_ud_ratio(market_lines)

    for row in rows:
        ud_line, pp_line = match_lines(row["name"], market_lines, pp_ud_ratio)
        if ud_line is None and pp_line is None:
            row["market_anchored"] = False
            continue

        mc = market_corrections.get(str(row.get("player_id")))
        if mc:
            ud_line = ud_line * mc["ud"]
            pp_line = pp_line * mc["pp"]

        raw_ud, raw_pp = row["ud_pts"], row["pp_pts"]

        ud_edge = max(-MARKET_EDGE_CLAMP, min(MARKET_EDGE_CLAMP, raw_ud - ud_line))
        row["ud_pts"] = round(ud_line + ud_edge, 2)
        row["ud_line"] = round(ud_line, 2)
        row["edge"] = round(ud_edge, 2)

        pp_edge = max(-MARKET_EDGE_CLAMP, min(MARKET_EDGE_CLAMP, raw_pp - pp_line))
        row["pp_pts"] = round(pp_line + pp_edge, 2)
        row["pp_line"] = round(pp_line, 2)

        row["market_anchored"] = True
        anchored.add(str(row.get("player_id")))

    return anchored


VALUE_PLAYS_DIR = os.path.join("data", "value_plays")


def save_value_plays(date_str, value_rows):
    """Persist today's Value Plays calls so tracker.py can grade OVER/UNDER
    accuracy once actual results are in."""
    plays = []
    for row in value_rows:
        plays.append({
            "player_id": row.get("player_id"),
            "name": row["name"],
            "team": row["team"],
            "call": "over" if row["edge"] > 0 else "under",
            "edge": row["edge"],
            "ud_line": row.get("ud_line"),
        })

    os.makedirs(VALUE_PLAYS_DIR, exist_ok=True)
    out_path = os.path.join(VALUE_PLAYS_DIR, f"{date_str}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "plays": plays}, f, indent=2)


PREMIUM_PLAYS_DIR = os.path.join("data", "premium_plays")
# Premium tier retired 2026-08-18 (see slips.py) - save_premium_plays()
# removed since nothing calls it anymore. PREMIUM_PLAYS_DIR stays defined
# because tracker.grade_premium_plays still references it (kept but
# unwired, same as the tier itself).


# ---------------------------------------------------------------------------
# Per-player model correction
#
# tracker.py records projected vs. actual UD/PP points for every graded
# player-day in data/results/all_results.json. Once a player has enough
# graded games, we know whether our projection has been running hot or cold
# for them specifically, and nudge future projections by that ratio.
# ---------------------------------------------------------------------------

MIN_GRADED_GAMES = 10
CORRECTION_CLAMP = (0.8, 1.2)  # avoid wild swings from small/noisy samples


def build_corrections(results_data, min_games=MIN_GRADED_GAMES):
    """Return {player_id_str: {"ud": factor, "pp": factor}} for players with
    enough graded history (avg actual / avg projected, clamped).

    Players without enough live-tracked history yet fall back to season-long
    correction factors from the backtest (results_data["backtest_corrections"])."""
    corrections = {}
    lo, hi = CORRECTION_CLAMP
    results_data = results_data or {}

    for pid, entry in (results_data.get("players", {}) or {}).items():
        history = entry.get("history", [])
        if len(history) < min_games:
            continue

        ud_proj = sum(h["projected_ud"] for h in history)
        ud_actual = sum(h["actual_ud"] for h in history)
        pp_proj = sum(h["projected_pp"] for h in history)
        pp_actual = sum(h["actual_pp"] for h in history)

        ud_factor = ud_actual / ud_proj if ud_proj > 0 else 1.0
        pp_factor = pp_actual / pp_proj if pp_proj > 0 else 1.0

        corrections[str(pid)] = {
            "ud": round(min(max(ud_factor, lo), hi), 3),
            "pp": round(min(max(pp_factor, lo), hi), 3),
        }

    for pid, c in (results_data.get("backtest_corrections", {}) or {}).items():
        if pid not in corrections:
            corrections[pid] = {
                "ud": round(min(max(c["ud"], lo), hi), 3),
                "pp": round(min(max(c["pp"], lo), hi), 3),
            }

    return corrections


def apply_corrections(rows, corrections, skip=None):
    """Apply each player's personal correction factor to ud_pts/pp_pts in
    place and flag row['adjusted'] so the dashboard can badge it. Players in
    `skip` (already anchored to a posted market line) are left untouched."""
    skip = skip or set()
    for row in rows:
        if str(row.get("player_id")) in skip:
            row["adjusted"] = False
            continue
        c = corrections.get(str(row.get("player_id")))
        if not c:
            row["adjusted"] = False
            continue
        row["ud_pts"] = round(row["ud_pts"] * c["ud"], 2)
        row["pp_pts"] = round(row["pp_pts"] * c["pp"], 2)
        row["adjusted"] = True


def apply_no_line_penalty(rows, anchored_ids=None):
    """Apply a flat projection discount to every player without a posted
    UD/PP market line. The absence of a line is itself a signal — UD
    withholds lines when a player's lineup status is uncertain — so we
    reduce their projection by NO_LINE_PENALTY to make it harder for them
    to crowd out market-confirmed players in the Top 25.

    An additional GETAWAY_DAY_PENALTY is layered on top when the player
    also has lineup_confirmed=False AND an early game start (before 18:00
    UTC / 11 AM PT). These two signals compounding each other represents
    the highest-risk profile — market silence + unconfirmed lineup on a
    morning getaway game.

    Sets row['no_line_penalty'] and row['getaway_day_risk'] flags so the
    dashboard can badge them appropriately."""
    anchored_ids = anchored_ids or set()
    for row in rows:
        if str(row.get("player_id")) in anchored_ids or row.get("market_anchored"):
            row["no_line_penalty"] = False
            row["getaway_day_risk"] = False
            continue

        row["ud_pts"] = round(row["ud_pts"] * (1 - NO_LINE_PENALTY), 2)
        row["pp_pts"] = round(row["pp_pts"] * (1 - NO_LINE_PENALTY), 2)
        row["no_line_penalty"] = True

        unconfirmed = not row.get("lineup_confirmed", True)
        early = _is_early_game(row.get("game_date_utc"))
        if unconfirmed and early:
            row["ud_pts"] = round(row["ud_pts"] * (1 - GETAWAY_DAY_PENALTY), 2)
            row["pp_pts"] = round(row["pp_pts"] * (1 - GETAWAY_DAY_PENALTY), 2)
            row["getaway_day_risk"] = True
        else:
            row["getaway_day_risk"] = False


def apply_venue_penalty(rows):
    """Reduce raw projections for venues and teams where the model has
    historically over-estimated performance vs. posted market lines.
    Applied BEFORE market anchoring so the adjustment compresses the edge
    (reducing OVER confidence or flipping marginal calls to UNDER) rather
    than shifting the displayed projection post-anchor."""
    for row in rows:
        venue_pen = VENUE_PROJECTION_PENALTIES.get(row.get("venue", ""), 0)
        team_pen  = TEAM_PROJECTION_PENALTIES.get(row.get("team", ""), 0)
        # Stack penalties multiplicatively so neither alone is outsized
        combined = 1 - (1 - venue_pen) * (1 - team_pen)
        if combined:
            row["ud_pts"] = round(row["ud_pts"] * (1 - combined), 2)
            row["pp_pts"] = round(row["pp_pts"] * (1 - combined), 2)


def fmt_value(val, kind):
    if val is None or val == "":
        return "N/A"
    if kind == "i":
        return int(val)
    if kind == "1f":
        return round(float(val), 1)
    if kind == "2f":
        return round(float(val), 2)
    if kind == "3f":
        return round(float(val), 3)
    return val




# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

DASHBOARD_COLS = [
    ("rank",        "Rank",         "i"),
    ("name",        "Player",       "s"),
    ("team",        "Team",         "s"),
    ("game_time_pt","Game Time",    "s"),
    ("order",       "Order",        "i"),
    ("ud_pts",      "UD Pts",       "1f"),
    ("pp_pts",      "PP Pts",       "1f"),
    ("xwoba",       "xwOBA",        "3f"),
    ("barrel_pct",  "Barrel%",      "1f"),
    ("hard_hit_pct","Hard Hit%",    "1f"),
    ("opp_era",     "Opp ERA",      "2f"),
    ("opp_fip",     "Opp FIP",      "2f"),
    ("weather",     "Weather",      "s"),
    ("park_hr",     "Park Factor",  "2f"),
    ("days_rest",   "Days Rest",    "i"),
    ("platoon_edge","Platoon Edge", "s"),
]


def write_dashboard(rows, date_str, out_path, results_data=None, top25_data=None):
    games_count = len({r["game_pk"] for r in rows})
    generated_dt = datetime.now(timezone.utc).astimezone(_PACIFIC)
    last_updated = generated_dt.strftime("%Y-%m-%d %I:%M %p PT")
    generated_at_iso = generated_dt.isoformat()
    player_count = len(rows)

    results_data = results_data or {"dates": {}, "players": {}}
    top25_data = top25_data or {"dates": {}, "players": {}}

    # --- Results: last 30 days calendar heatmap ------------------------
    result_dates = sorted(results_data.get("dates", {}).keys())[-30:]
    calendar_cells = []
    total_ud_hit = total_ud = total_pp_hit = total_pp = 0
    for d in result_dates:
        s = results_data["dates"][d]
        calendar_cells.append({
            "date": d,
            "ud_hit_rate": s["ud"]["hit_rate"],
            "pp_hit_rate": s["pp"]["hit_rate"],
            "player_count": s["player_count"],
            "lowCoverage": d in LOW_COVERAGE_DATES,
            "lowCoverageReason": LOW_COVERAGE_DATES.get(d),
        })
        total_ud_hit += s["ud"]["win"]
        total_ud += s["ud"]["total"]
        total_pp_hit += s["pp"]["win"]
        total_pp += s["pp"]["total"]
    calendar_js = json.dumps(calendar_cells)
    overall_ud_rate = round(100 * total_ud_hit / total_ud, 1) if total_ud else 0.0
    overall_pp_rate = round(100 * total_pp_hit / total_pp, 1) if total_pp else 0.0

    # --- Player History --------------------------------------------------
    player_history_js = json.dumps(results_data.get("players", {}))

    # --- Top 25 Results --------------------------------------------------
    t25_dates = sorted(top25_data.get("dates", {}).keys())
    last7_dates = t25_dates[-7:]

    yesterday_cards = []
    daily_hit_rate = None
    daily_date = None
    top25_players = top25_data.get("players", {})
    if t25_dates:
        daily_date = t25_dates[-1]
        yesterday_cards = [
            {**e, "top25Record": top25_record_badge(e.get("player_id"), top25_players)}
            for e in top25_data["dates"][daily_date]["top25"]
        ]
        decided = [e for e in yesterday_cards if e["grade"] in ("win", "loss")]
        if decided:
            hits = sum(1 for e in decided if e["grade"] == "win")
            daily_hit_rate = round(100 * hits / len(decided), 1)
    yesterday_cards_js = json.dumps(yesterday_cards)

    rolling_hits = rolling_decided = 0
    for d in last7_dates:
        for e in top25_data["dates"][d]["top25"]:
            if e["grade"] in ("win", "loss"):
                rolling_decided += 1
                if e["grade"] == "win":
                    rolling_hits += 1
    rolling_hit_rate = round(100 * rolling_hits / rolling_decided, 1) if rolling_decided else None

    record_rows = []
    weekly = []
    trend_symbols = {"win": "✓", "loss": "✗", "push": "~"}
    for pid, p in top25_data.get("players", {}).items():
        history = p.get("history", [])
        appearances = len(history)
        if appearances == 0:
            continue
        hits = sum(1 for h in history if h["grade"] == "win")
        losses = sum(1 for h in history if h["grade"] == "loss")
        decided = hits + losses
        hit_rate = round(100 * hits / decided, 1) if decided else 0.0
        avg_proj = round(sum(h["projected_ud"] for h in history) / appearances, 2)
        avg_actual = round(sum(h["actual_ud"] for h in history) / appearances, 2)
        trend = "".join(trend_symbols[h["grade"]] for h in history[-5:])
        record_rows.append({
            "name": p.get("name", ""),
            "team": p.get("team", ""),
            "times": len(p.get("dates_seen", [])) or appearances,
            "record": f"{hits}-{losses}",
            "hit_rate": hit_rate,
            "avg_proj": avg_proj,
            "avg_actual": avg_actual,
            "trend": trend,
        })

        recent_decided = [h for h in history if h["date"] in last7_dates and h["grade"] in ("win", "loss")]
        if len(recent_decided) >= 2:
            r_hits = sum(1 for h in recent_decided if h["grade"] == "win")
            weekly.append({
                "name": p.get("name", ""),
                "rate": round(100 * r_hits / len(recent_decided), 1),
                "n": len(recent_decided),
            })

    record_rows.sort(key=lambda r: r["hit_rate"], reverse=True)
    record_rows_js = json.dumps(record_rows)

    best_performer = max(weekly, key=lambda x: x["rate"]) if weekly else None
    worst_performer = min(weekly, key=lambda x: x["rate"]) if weekly else None

    # Top 25 Results - full-history calendar + all-time summary, covering
    # every tracked date (not just yesterday/last 7 days like the summary
    # cards above). Same day-cell shape as the general Results tab's
    # calendar so it reuses that CSS/rendering pattern.
    t25_calendar_cells = []
    t25_all_hits = t25_all_decided = 0
    for d in t25_dates:
        day_decided = [e for e in top25_data["dates"][d]["top25"] if e["grade"] in ("win", "loss")]
        day_hits = sum(1 for e in day_decided if e["grade"] == "win")
        t25_calendar_cells.append({
            "date": d,
            "hit_rate": round(100 * day_hits / len(day_decided), 1) if day_decided else None,
            "n": len(day_decided),
        })
        t25_all_hits += day_hits
        t25_all_decided += len(day_decided)
    t25_calendar_js = json.dumps(t25_calendar_cells)
    t25_all_time_rate = round(100 * t25_all_hits / t25_all_decided, 1) if t25_all_decided else None

    # --- Fire/Hot Marked Plays cohorts (UNVALIDATED - see
    # tracker.track_marked_plays). Point-in-time reconstruction lives in
    # tracker.py; this just reads its output and shapes it for display,
    # using the same daily-calendar domain (t25_dates) as the Top 25
    # Results calendar above so every tracked date gets a cell even on
    # days with no qualifying fire/hot plays. -----------------------------
    marked_data = {"dates": {}, "cohorts": {}}
    if os.path.exists(MARKED_PLAYS_RESULTS_PATH):
        try:
            with open(MARKED_PLAYS_RESULTS_PATH, encoding="utf-8") as f:
                marked_data = json.load(f)
        except Exception:
            pass

    def _mark_calendar(mark):
        cells = []
        for d in t25_dates:
            day = marked_data.get("dates", {}).get(d, {}).get(mark, [])
            wins = sum(1 for e in day if e.get("grade") == "win")
            losses = sum(1 for e in day if e.get("grade") == "loss")
            n = wins + losses
            cells.append({
                "date": d, "wins": wins, "losses": losses, "n": n,
                "rate": round(100 * wins / n, 1) if n else None,
            })
        return cells

    def _mark_summary(mark):
        c = marked_data.get("cohorts", {}).get(mark) or {"wins": 0, "losses": 0, "n": 0, "rate": None}
        wins, losses, n = c.get("wins", 0), c.get("losses", 0), c.get("n", 0)
        ci_lo, ci_hi = wilson_ci(wins, n)
        return {"wins": wins, "losses": losses, "n": n, "rate": c.get("rate"),
                "ci_lo": ci_lo, "ci_hi": ci_hi}

    fire_cal_js = json.dumps(_mark_calendar("fire"))
    hot_cal_js = json.dumps(_mark_calendar("hot"))
    fire_summary_js = json.dumps(_mark_summary("fire"))
    hot_summary_js = json.dumps(_mark_summary("hot"))

    # Default display order for every card grid is "soonest game first",
    # which is independent of (and applied after) whatever scoring/edge
    # logic decided which players make a given section - missing game
    # times sort last rather than first.
    def _by_game_time(row_list):
        return sorted(row_list, key=lambda r: r.get("game_date_utc") or "9999")

    # --- Top 25 cards -------------------------------------------------
    cards = [build_card(row) for row in _by_game_time(rows[:25])]
    for c in cards:
        c["top25Record"] = top25_record_badge(c.get("playerId"), top25_players)
    # cards_js is serialized further below, after value_rows is known, so
    # valuePlay membership can be added.

    # --- Value Plays: model vs. market disagreement above threshold ---------
    # Live thresholds tracked in data/results/edge_bucket_rates.json
    # (refreshed weekly by tracker.py from clean graded data — see
    # slips.refresh_edge_bucket_rates). Snapshot as of 2026-07-13:
    #   OVERs  >= 1.0 pt: 54.3% win on the 1.0-1.5 bucket
    #   UNDERs >= 1.5 pt: 59.4% win on the 1.5-2.0 bucket
    # UNDER suppression: opp SP ERA 4.5-5.5 = 32.9% win rate (-19.2% ROI, n=70).
    # Bad pitchers give up points — UNDER calls vs their opponents are unsound.
    # Only players with a posted UD/PP line count. Capped at top 4 each.
    def _bad_era_for_under(row):
        era = row.get("opp_era")
        return era is not None and 4.5 <= era < 5.5

    over_rows = [r for r in rows if r.get("market_anchored") and (r.get("edge") or 0) >= VALUE_PLAY_OVER_EDGE]
    under_rows = [r for r in rows if r.get("market_anchored")
                  and (r.get("edge") or 0) <= -VALUE_PLAY_UNDER_EDGE
                  and not _bad_era_for_under(r)]
    over_rows.sort(key=lambda r: r["edge"], reverse=True)
    under_rows.sort(key=lambda r: r["edge"])
    value_rows = over_rows[:4] + under_rows[:4]
    save_value_plays(date_str, value_rows)
    value_cards = [build_card(row) for row in _by_game_time(value_rows)]
    value_cards_js = json.dumps(value_cards)

    # --- Unanchored: no posted UD/PP line, model-only projection ----------
    unanchored_rows = _by_game_time([r for r in rows[:25] if not r.get("market_anchored")])
    unanchored_cards = [build_card(row) for row in unanchored_rows]
    unanchored_cards_js = json.dumps(unanchored_cards)

    # --- Unders tab: ONLY UD UNDER calls in the validated 1.5-2.0 edge band
    # (in_ud_under_band, see module comment above) - unvalidated unders are
    # noise that just bury the one replicated signal, so they're dropped
    # entirely rather than shown unstyled. Sorted by edge size descending -
    # biggest model-vs-market disagreement first. No top25Record here (it's
    # a Top 25 appearance stat, irrelevant/misleading for a tab that isn't
    # about Top 25 membership at all). -------------------------------------
    unders_all_rows = [r for r in rows if r.get("market_anchored") and (r.get("edge") or 0) < 0]
    unders_total_count = len(unders_all_rows)
    unders_rows = [r for r in unders_all_rows if in_ud_under_band(r)]
    unders_rows.sort(key=lambda r: r["edge"])  # most negative (biggest edge) first
    unders_cards = [build_card(row) for row in unders_rows]
    unders_cards_js = json.dumps(unders_cards)
    unders_band_count = len(unders_cards)
    save_ud_under_band_plays(date_str, unders_rows)

    # --- Under Results tab: grading history for the validated band, built
    # from data/results/ud_under_band_results.json (tracker.grade_ud_under_band,
    # graded nightly). DNPs are tracked separately and never counted as
    # wins/losses - see grade_ud_under_band's DNP handling. -----------------
    under_band_data = {"seed": UD_UNDER_BAND_SEED, "dates": {}, "record": {"wins": 0, "losses": 0, "dnp": 0}}
    if os.path.exists(UD_UNDER_BAND_RESULTS_PATH):
        try:
            with open(UD_UNDER_BAND_RESULTS_PATH, encoding="utf-8") as f:
                under_band_data = json.load(f)
        except Exception:
            pass
    under_dates = sorted(under_band_data.get("dates", {}).keys())
    under_calendar_cells = []
    for d in under_dates:
        dd = under_band_data["dates"][d]
        w, l, dnp = dd.get("wins", 0), dd.get("losses", 0), dd.get("dnp", 0)
        decided = w + l
        under_calendar_cells.append({
            "date": d,
            "hit_rate": round(100 * w / decided, 1) if decided else None,
            "wins": w, "losses": l, "dnp": dnp,
            "lowCoverage": bool(dd.get("low_coverage")),
            "lowCoverageReason": dd.get("low_coverage_reason"),
        })
    under_calendar_js = json.dumps(under_calendar_cells)

    under_yesterday_date = under_dates[-1] if under_dates else None
    under_yesterday_cards_js = json.dumps(
        under_band_data["dates"][under_yesterday_date]["plays"] if under_yesterday_date else []
    )

    under_live_record = under_band_data.get("record", {"wins": 0, "losses": 0, "dnp": 0})
    under_total_wins = UD_UNDER_BAND_SEED["wins"] + under_live_record.get("wins", 0)
    under_total_losses = UD_UNDER_BAND_SEED["losses"] + under_live_record.get("losses", 0)
    under_total_dnp = under_live_record.get("dnp", 0)  # seed predates DNP tracking - live-tracked only
    under_total_n = under_total_wins + under_total_losses
    under_total_rate = round(100 * under_total_wins / under_total_n, 1) if under_total_n else 0.0
    under_ci_lo, under_ci_hi = wilson_ci(under_total_wins, under_total_n)

    # --- Top 25 tier-membership badge (VALUE PLAY only now - PREMIUM/
    # STRONG/SLIP/STACK retired 2026-08-18, see module docstrings) ---
    value_pids = {r.get("player_id") for r in value_rows}
    for c in cards:
        c["valuePlay"] = c.get("playerId") in value_pids
    cards_js = json.dumps(cards)

    # --- Full leaderboard table ---------------------------------------
    # Color tiers are relative to today's own distribution (top 25% green,
    # next down to the 40th percentile yellow, rest red) rather than fixed
    # point thresholds - ud_pts now lives on the recalibrated/anchored
    # 3.5-9.5 scale, not the old raw model scale those fixed cutoffs were
    # tuned for, so an absolute "8+" cutoff left almost nothing green.
    cols_js = json.dumps([{"key": k, "label": label} for k, label, _ in DASHBOARD_COLS])
    ud_values = [r.get("ud_pts") or 0 for r in rows]
    full_thresholds = (percentile(ud_values, 75), percentile(ud_values, 40))
    table_rows = []
    for rank, row in enumerate(rows, 1):
        cells = [fmt_value(rank if key == "rank" else row[key], kind)
                 for key, _, kind in DASHBOARD_COLS]
        color = "row-" + tier(row.get("ud_pts") or 0, full_thresholds)
        table_rows.append({
            "team": row["team"],
            "cells": cells,
            "color": color,
            "gameDateUtc": row.get("game_date_utc"),
            "underBand": in_ud_under_band(row),
        })
    # Default table order is soonest game first, independent of rank.
    table_rows.sort(key=lambda r: r["gameDateUtc"] or "9999")
    rows_js = json.dumps(table_rows)

    teams = sorted({r["team"] for r in rows})
    teams_js = json.dumps(teams)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>MLB Fantasy Projections — {date_str}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 0; background: #0d1626; color: #e6e9f0; }}

  header {{ background: #0a1628; padding: 18px 24px; display: flex; align-items: center;
            justify-content: space-between; flex-wrap: wrap; gap: 8px;
            border-bottom: 1px solid #1f2c46; }}
  .logo {{ font-size: 22px; font-weight: 800; color: #fff; letter-spacing: 0.5px; }}
  .header-mid {{ font-size: 14px; color: #9fb0cc; text-align: center; }}
  .header-mid .games {{ font-weight: 700; color: #fff; }}
  .header-right {{ font-size: 12px; color: #6c7da0; text-align: right; }}

  .filter-bar {{ padding: 14px 24px 0; display: flex; justify-content: flex-end; }}
  .filter-toggle {{ font-size: 13px; color: #c4cee0; display: flex; align-items: center; gap: 6px; cursor: pointer; }}
  .filter-toggle input {{ width: 15px; height: 15px; cursor: pointer; }}
  .game-time {{ color: #fbbf24; }}

  .tabs {{ padding: 14px 24px 0; display: flex; gap: 8px; }}
  .tab-btn {{ padding: 9px 20px; font-size: 14px; border: 1px solid #2a3a5c; background: #16213a;
              color: #c4cee0; cursor: pointer; border-radius: 8px 8px 0 0; }}
  .tab-btn.active {{ background: #1c2944; color: #fff; border-bottom-color: #1c2944; font-weight: 600; }}

  .panel {{ padding: 18px 24px; }}
  .panel.hidden {{ display: none; }}

  .legend {{ margin-bottom: 14px; font-size: 12px; color: #9fb0cc; }}
  .legend span {{ display: inline-block; padding: 3px 12px; margin-right: 8px; border-radius: 4px; font-weight: 600; }}
  .legend .green  {{ background: #15351f; color: #4ade80; border: 1px solid #4ade80; }}
  .legend .yellow {{ background: #3a3315; color: #fbbf24; border: 1px solid #fbbf24; }}
  .legend .red    {{ background: #3a1818; color: #f87171; border: 1px solid #f87171; }}

  /* Top 25 card grid */
  .card-grid {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; }}
  @media (max-width: 1400px) {{ .card-grid {{ grid-template-columns: repeat(3, 1fr); }} }}
  @media (max-width: 900px)  {{ .card-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
  @media (max-width: 600px)  {{ .card-grid {{ grid-template-columns: 1fr; }} }}

  .card {{ background: #16213a; border-radius: 10px; padding: 14px 16px; border: 3px solid #555;
           transition: transform 0.15s ease, box-shadow 0.15s ease; position: relative; }}
  .card:hover {{ transform: scale(1.04); box-shadow: 0 6px 20px rgba(0,0,0,0.4); z-index: 2; }}
  .card.green  {{ border-color: #4ade80; }}
  .card.yellow {{ border-color: #fbbf24; }}
  .card.red    {{ border-color: #f87171; }}

  /* Top 25 hit-rate treatment - only for players with enough appearances
     (n) behind their rate to mean something; see top25TreatmentClass().
     .top25-hot (n 8-14, 55%+): solid border, no glow - promising but thin.
     .top25-fire (n 15+, 55%+): animated gradient glow - the real signal. */
  .card.top25-hot {{ border-color: #fb923c; }}
  .card.top25-fire {{
    border: 3px solid transparent;
    background-image: linear-gradient(#16213a, #16213a),
                       linear-gradient(135deg, #f97316, #fbbf24, #f97316);
    background-origin: border-box;
    background-clip: padding-box, border-box;
    animation: top25FireGlow 2.2s ease-in-out infinite;
  }}
  @keyframes top25FireGlow {{
    0%, 100% {{ box-shadow: 0 0 8px 1px rgba(249,115,22,0.45); }}
    50%      {{ box-shadow: 0 0 20px 5px rgba(249,115,22,0.85); }}
  }}

  .unvalidated-badge {{ display: inline-block; margin-left: 8px; padding: 2px 8px; font-size: 11px;
                         font-weight: 800; letter-spacing: 0.5px; color: #fb923c;
                         border: 1px solid #fb923c; border-radius: 10px; vertical-align: middle; }}
  .unvalidated-note {{ font-size: 12px; color: #fb923c; background: #2a1c0d; border: 1px solid #7c4a1e;
                        border-radius: 6px; padding: 8px 12px; margin-bottom: 14px; }}
  .cohort-label {{ font-size: 13px; font-weight: 700; color: #c4cee0; margin: 14px 0 8px; }}

  /* Click-to-mark-used: dims the card so the eye skips it while scanning,
     without reordering or hiding anything. Purely a client-side visual
     toggle persisted in localStorage - see toggleUsed()/usedStorageKey.
     Scoped to the four picks grids only (cardGrid/undersGrid/valueGrid/
     unanchoredGrid) - t25CardGrid's historical result cards aren't wired
     to a click handler, so they must not get the pointer cursor either,
     or they'd look clickable and do nothing. */
  #cardGrid .card, #undersGrid .card, #valueGrid .card, #unanchoredGrid .card {{ cursor: pointer; }}
  .card.used {{ opacity: 0.4; filter: grayscale(0.85); }}
  .card.used:hover {{ opacity: 0.6; }}
  .card.used::after {{
    content: '\2713'; position: absolute; top: 8px; right: 10px;
    width: 22px; height: 22px; border-radius: 50%;
    background: rgba(74,222,128,0.9); color: #0d1626;
    font-size: 14px; font-weight: 900; line-height: 22px; text-align: center;
  }}
  .clear-marks-btn {{ padding: 5px 12px; font-size: 12px; font-weight: 600; border: 1px solid #3a4866;
                       background: #16213a; color: #9fb0cc; border-radius: 6px; cursor: pointer;
                       margin-bottom: 12px; }}
  .clear-marks-btn:hover {{ background: #1c2944; color: #fff; border-color: #6c7da0; }}

  .card .name {{ font-size: 18px; font-weight: 800; color: #fff; }}
  .card .meta {{ font-size: 12px; color: #9fb0cc; margin-top: 2px; }}
  .card .pts-row {{ margin-top: 10px; display: flex; gap: 18px; align-items: baseline; }}
  .card .ud-pts {{ font-size: 26px; font-weight: 800; color: #4ade80; }}
  .card .pp-pts {{ font-size: 18px; font-weight: 700; color: #60a5fa; }}
  .card .line-pts {{ font-size: 18px; font-weight: 700; color: #9fb0cc; }}
  .card .pts-label {{ font-size: 10px; color: #6c7da0; display: block; }}
  .t25-call {{ display: inline-flex; align-items: center; gap: 4px; margin-top: 8px; padding: 2px 8px;
               border-radius: 10px; font-weight: 800; font-size: 12px; }}
  .t25-call.over  {{ background: rgba(96,165,250,0.15); color: #60a5fa; }}
  .t25-call.under {{ background: rgba(251,146,60,0.15); color: #fb923c; }}
  .card .stat-line {{ font-size: 12px; color: #c4cee0; margin-top: 8px; }}
  .card .badge {{ display: inline-block; margin-top: 8px; padding: 2px 8px; font-size: 11px;
                  font-weight: 700; color: #0d1626; background: #4ade80; border-radius: 10px; }}
  .card .badge-adjusted {{ background: #60a5fa; margin-left: 6px; }}
  .card .badge-anchored {{ background: #f0abfc; margin-left: 6px; }}
  .card .badge-projected {{ background: #fbbf24; color: #3a2a00; margin-left: 6px; }}
  .card .badge-no-line {{ background: #fb923c; color: #1a0800; margin-left: 6px; }}
  .card .badge-getaway {{ background: #f87171; color: #1a0000; margin-left: 6px; }}
  .card .badge-value  {{ background: #2dd4bf; margin-left: 6px; }}
  .card .badge-under-band {{ background: transparent; border: 1px solid #fb923c; color: #fb923c;
                              margin-left: 6px; font-weight: 700; }}
  .card .top25-record {{ font-size: 12px; color: #9fb0cc; margin-top: 6px; }}
  .card .top25-record.has-rate {{ color: #c4cee0; }}

  /* Full leaderboard table */
  .controls {{ display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }}
  .controls input, .controls select {{ padding: 8px 12px; font-size: 14px; background: #16213a;
              color: #e6e9f0; border: 1px solid #2a3a5c; border-radius: 6px; }}
  .table-wrap {{ max-height: 75vh; overflow: auto; border: 1px solid #2a3a5c; border-radius: 8px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ padding: 8px 12px; font-size: 13px; text-align: left; white-space: nowrap; }}
  th {{ background: #1c2944; color: #fff; cursor: pointer; position: sticky; top: 0; user-select: none;
        border-bottom: 2px solid #2a3a5c; }}
  th:hover {{ background: #25355a; }}
  th.sorted-asc::after {{ content: " \\25B2"; }}
  th.sorted-desc::after {{ content: " \\25BC"; }}
  tbody tr:nth-child(odd)  {{ background: #141d33; }}
  tbody tr:nth-child(even) {{ background: #182241; }}
  tbody tr:hover {{ background: #25355a; }}
  td {{ border-bottom: 1px solid #1f2c46; color: #d6deef; }}

  /* Results tab */
  .results-summary {{ display: flex; gap: 14px; margin-bottom: 16px; }}
  .summary-card {{ background: #16213a; border: 1px solid #2a3a5c; border-radius: 10px;
                   padding: 14px 22px; text-align: center; }}
  .summary-value {{ font-size: 28px; font-weight: 800; color: #fff; }}
  .summary-label {{ font-size: 12px; color: #9fb0cc; margin-top: 4px; }}
  .cal-grid {{ display: flex; flex-wrap: wrap; gap: 8px; }}
  .cal-cell {{ width: 78px; height: 58px; background: #16213a; border: 2px solid #555;
               border-radius: 8px; display: flex; flex-direction: column; align-items: center;
               justify-content: center; }}
  .cal-date {{ font-size: 11px; color: #9fb0cc; }}
  .cal-rate {{ font-size: 16px; font-weight: 800; color: #fff; margin-top: 2px; }}
  /* Wider variant for calendars that show a full "W-L (rate%)" record
     instead of a bare percentage - see underCalGrid/fireCalGrid/hotCalGrid. */
  .cal-cell.wide {{ width: 112px; }}
  .cal-cell.wide .cal-rate {{ font-size: 12px; }}
  /* Low-coverage day (thin UD board - see report.LOW_COVERAGE_DATES):
     hatched background so a thin/unrepresentative day reads as visibly
     different at a glance, not just a differently-colored border. */
  .cal-cell.low-coverage {{
    position: relative;
    background-image: repeating-linear-gradient(135deg, #3a2a15, #3a2a15 6px, #16213a 6px, #16213a 12px);
    border-color: #fb923c !important;
  }}
  .cal-cell.low-coverage::after {{
    content: '\26A0'; position: absolute; top: 2px; right: 4px;
    font-size: 10px; color: #fb923c;
  }}
  .empty-msg {{ color: #9fb0cc; font-size: 14px; }}

  /* Player History tab */
  .ph-player {{ background: #16213a; border: 1px solid #2a3a5c; border-radius: 10px;
                padding: 14px 18px; margin-bottom: 14px; }}
  .ph-player h3 {{ margin: 0 0 10px; color: #fff; font-size: 16px; }}
  .ph-player h3 .meta {{ color: #9fb0cc; font-size: 12px; font-weight: 400; }}
  .ph-table {{ width: 100%; border-collapse: collapse; }}
  .ph-table th, .ph-table td {{ padding: 6px 10px; font-size: 13px; text-align: left; white-space: nowrap; }}
  .ph-table th {{ color: #9fb0cc; border-bottom: 1px solid #2a3a5c; position: static; cursor: default; }}
  .ph-table td {{ color: #d6deef; border-bottom: 1px solid #1f2c46; }}
  .res-win   {{ color: #4ade80; font-weight: 700; }}
  .res-loss  {{ color: #f87171; font-weight: 700; }}
  .res-push  {{ color: #9fb0cc; font-weight: 700; }}
  .res-none  {{ color: #6c7da0; font-weight: 700; }}

  /* Top 25 Results tab */
  .summary-card.best  {{ border-color: #4ade80; }}
  .summary-card.worst {{ border-color: #f87171; }}
  .t25-card.win    {{ border-color: #4ade80; }}
  .t25-card.push   {{ border-color: #fbbf24; }}
  .t25-card.loss   {{ border-color: #f87171; }}
  .t25-card.nodata {{ border-color: #555; }}
  .t25-overlay {{ position: absolute; top: 6px; right: 10px; font-size: 30px; font-weight: 900; }}
  .t25-overlay.win  {{ color: #4ade80; }}
  .t25-overlay.push {{ color: #fbbf24; }}
  .t25-overlay.loss {{ color: #f87171; }}
  .t25-overlay.dnp  {{ color: #9fb0cc; font-size: 13px; letter-spacing: 0.5px; }}
  .section-title {{ margin: 22px 0 12px; color: #fff; font-size: 16px; }}
  #t25Tbl tbody tr.row-green  td {{ background: #15351f; }}
  #t25Tbl tbody tr.row-yellow td {{ background: #3a3315; }}
  #t25Tbl tbody tr.row-red    td {{ background: #3a1818; }}
  /* Full Leaderboard color rows */
  #tbl tbody tr.row-green  td {{ background: #0d2418; }}
  #tbl tbody tr.row-yellow td {{ background: #2a240e; }}
  #tbl tbody tr.row-red    td {{ background: #2a1010; }}
  #tbl tbody tr.row-green:hover td  {{ background: #15351f; }}
  #tbl tbody tr.row-yellow:hover td {{ background: #3a3315; }}
  #tbl tbody tr.row-red:hover td    {{ background: #3a1818; }}
  /* UD UNDER 1.5-2.0 edge band marker - raw bucket stat, not a tier */
  #tbl tbody tr.under-band-row td:first-child {{ border-left: 3px solid #fb923c; }}
  #tbl tbody tr.under-band-row td:first-child::after {{
    content: " \25BC"; color: #fb923c; font-size: 10px; }}
  .bt-bar-row {{ display: flex; align-items: center; gap: 10px; margin: 6px 0; }}
  .bt-bar-label {{ width: 160px; font-size: 13px; color: #c4cee0; flex-shrink: 0; }}
  .bt-bar {{ flex: 1; height: 14px; background: #16213a; border: 1px solid #2a3a5c; border-radius: 4px; overflow: hidden; }}
  .bt-bar-fill {{ height: 100%; background: #60a5fa; }}
  .bt-bar-value {{ width: 110px; font-size: 12px; color: #9fb0cc; text-align: right; flex-shrink: 0; }}
  .bt-grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-top: 12px; }}
  @media (max-width: 900px) {{ .bt-grid-2 {{ grid-template-columns: 1fr; }} }}

  /* Edge / Value Plays */
  .card .edge-row {{ margin-top: 8px; font-size: 12px; color: #9fb0cc; }}
  .edge-tag {{ display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px;
               border-radius: 10px; font-weight: 800; font-size: 12px; }}
  .edge-tag.over    {{ background: rgba(74,222,128,0.15); color: #4ade80; }}
  .edge-tag.under   {{ background: rgba(248,113,113,0.15); color: #f87171; }}
  .edge-tag.neutral {{ background: rgba(159,176,204,0.15); color: #9fb0cc; }}

  /* Platoon Matchup */
  .platoon-matchup {{ margin-top: 8px; padding: 8px 10px; border-radius: 8px; font-size: 11px;
                       border-left: 3px solid #9fb0cc; }}
  .platoon-matchup.edge-batter  {{ background: rgba(74,222,128,0.10); border-left-color: #4ade80; }}
  .platoon-matchup.edge-pitcher {{ background: rgba(248,113,113,0.10); border-left-color: #f87171; }}
  .platoon-matchup.edge-neutral {{ background: rgba(159,176,204,0.10); border-left-color: #9fb0cc; }}
  .platoon-title {{ font-weight: 800; margin-bottom: 4px; color: #cbd5e1; }}
  .platoon-row {{ display: flex; flex-direction: column; gap: 2px; color: #9fb0cc; }}

  .value-plays {{ margin: 16px 0 28px; padding: 16px; border: 2px solid #fbbf24;
                   border-radius: 12px; background: linear-gradient(180deg, rgba(251,191,36,0.08), transparent); }}
  .value-plays h2 {{ margin: 0 0 4px; color: #fbbf24; font-size: 20px; }}
  .value-plays .vp-sub {{ color: #c4cee0; font-size: 13px; margin-bottom: 12px; }}
  .value-plays .card {{ border-width: 2px; }}
  .value-plays-empty {{ color: #9fb0cc; font-size: 13px; }}

  .unanchored-section {{ margin: 16px 0 28px; padding: 16px; border: 1px solid #3a4866;
                          border-radius: 12px; }}
  .unanchored-section h2 {{ margin: 0 0 4px; color: #9fb0cc; font-size: 18px; }}
  .unanchored-section .vp-sub {{ color: #7a8aab; font-size: 13px; margin-bottom: 12px; }}
  .unanchored-empty {{ color: #9fb0cc; font-size: 13px; }}

  .unders-header {{ margin-bottom: 16px; padding: 10px 14px; border: 1px solid #3a4866;
                     border-radius: 8px; background: #16213a; }}
  .unders-header .unders-band-line {{ color: #fb923c; font-size: 13px; font-weight: 700; }}
  .unders-header .unders-count-line {{ color: #9fb0cc; font-size: 12px; margin-top: 4px; }}

  /* Freshness banner */
  .freshness-banner {{ padding: 10px 24px; font-size: 14px; font-weight: 700; text-align: center; }}
  .freshness-banner.fresh {{ background: #15351f; color: #4ade80; }}
  .freshness-banner.stale {{ background: #3a1818; color: #f87171; }}
  .card .game-date {{ color: #6c7da0; }}
</style>
</head>
<body>
<header>
  <div class="logo">⚾ MLB FANTASY PRO</div>
  <div class="header-mid">{date_str} &mdash; <span class="games">{games_count} games today</span></div>
  <div class="header-right" id="lastUpdated">Last Updated: {last_updated}</div>
</header>

<div id="freshnessBanner" class="freshness-banner"></div>

<div class="filter-bar">
  <label class="filter-toggle">
    <input type="checkbox" id="hideStartedToggle">
    Hide games already started
  </label>
</div>

<div class="value-plays">
  <h2>🎯 Value Plays</h2>
  <div class="vp-sub">Top 4 OVER calls (model disagrees by 1.0+ pts) + top 4 UNDER calls (1.5+ pts) &mdash; thresholds calibrated from live, weekly-refreshed edge-bucket win rates.</div>
  <button class="clear-marks-btn" data-clear-marks>Clear all marks</button>
  <div class="card-grid" id="valueGrid"></div>
</div>

<div class="unanchored-section">
  <h2>Unanchored (no posted line)</h2>
  <div class="vp-sub">Model-only projections &mdash; no UD/PP market line to compare against yet.</div>
  <button class="clear-marks-btn" data-clear-marks>Clear all marks</button>
  <div class="card-grid" id="unanchoredGrid"></div>
</div>

<div class="tabs">
  <button class="tab-btn active" id="tab-top25" data-tab="top25">Top 25</button>
  <button class="tab-btn" id="tab-unders" data-tab="unders">Unders</button>
  <button class="tab-btn" id="tab-full" data-tab="full">Full Leaderboard</button>
  <button class="tab-btn" id="tab-results" data-tab="results">Results</button>
  <button class="tab-btn" id="tab-history" data-tab="history">Player History</button>
  <button class="tab-btn" id="tab-top25results" data-tab="top25results">Top 25 Results</button>
  <button class="tab-btn" id="tab-underresults" data-tab="underresults">Under Results</button>
</div>

<div class="panel" id="panel-top25">
  <div class="legend">
    <span class="green">Green border = Elite (8+ pts)</span>
    <span class="yellow">Yellow = Solid (5-8 pts)</span>
    <span class="red">Red = Avoid (under 5 pts)</span>
  </div>
  <button class="clear-marks-btn" data-clear-marks>Clear all marks</button>
  <div class="card-grid" id="cardGrid"></div>
</div>

<div class="panel hidden" id="panel-unders">
  <div class="unders-header" id="undersHeader"></div>
  <button class="clear-marks-btn" data-clear-marks>Clear all marks</button>
  <div class="card-grid" id="undersGrid"></div>
</div>

<div class="panel hidden" id="panel-full">
  <div class="controls">
    <input id="search" type="text" placeholder="Search player name...">
    <select id="teamFilter">
      <option value="">All Teams</option>
    </select>
  </div>
  <div class="table-wrap">
    <table id="tbl">
      <thead><tr id="hdr"></tr></thead>
      <tbody id="body"></tbody>
    </table>
  </div>
</div>

<div class="panel hidden" id="panel-results">
  <div class="results-summary">
    <div class="summary-card">
      <div class="summary-value">{overall_ud_rate}%</div>
      <div class="summary-label">UD Hit Rate (last {len(result_dates)}d)</div>
    </div>
    <div class="summary-card">
      <div class="summary-value">{overall_pp_rate}%</div>
      <div class="summary-label">PP Hit Rate (last {len(result_dates)}d)</div>
    </div>
  </div>
  <div class="legend">
    <span class="green">Green = 50%+ hit rate</span>
    <span class="yellow">Yellow = 30-50%</span>
    <span class="red">Red = under 30%</span>
  </div>
  <div class="cal-grid" id="calGrid"></div>
</div>

<div class="panel hidden" id="panel-history">
  <div class="controls">
    <input id="phSearch" type="text" placeholder="Search player name...">
  </div>
  <div id="phResults"></div>
</div>

<div class="panel hidden" id="panel-top25results">
  <div class="results-summary" id="t25Summary"></div>
  <h3 class="section-title">Daily Hit Rate (every tracked date)</h3>
  <div class="cal-grid" id="t25CalGrid"></div>
  <div class="legend" style="margin-top:16px;">
    <span class="green">Green ✓ = won the call (right side of the UD line)</span>
    <span class="red">Red ✗ = lost the call (wrong side of the UD line)</span>
    <span class="yellow">Yellow ~ = push (landed exactly on the line)</span>
    <span style="background:#1c2944;color:#9fb0cc;border:1px solid #555;">Gray = no result yet</span>
  </div>
  <div class="card-grid" id="t25CardGrid"></div>

  <h3 class="section-title">Fire &amp; Hot Marked Plays <span class="unvalidated-badge">UNVALIDATED</span></h3>
  <div class="unvalidated-note">This cohort is selected by each player's own past Top 25 hit rate (fire = n&ge;15 appearances at 55%+; hot = n 8&ndash;14 at 55%+), so it is exposed to the same regression-to-the-mean risk that retired Premium. Treated and displayed honestly here, not as a proven edge, until it has real out-of-sample volume behind it.</div>
  <div class="cohort-label">Fire (n&ge;15, 55%+ historical rate)</div>
  <div class="results-summary" id="fireSummary"></div>
  <div class="cal-grid" id="fireCalGrid"></div>
  <div class="cohort-label">Hot (n 8&ndash;14, 55%+ historical rate)</div>
  <div class="results-summary" id="hotSummary"></div>
  <div class="cal-grid" id="hotCalGrid"></div>

  <h3 class="section-title">Running Record (all-time Top 25 appearances)</h3>
  <div class="table-wrap">
    <table id="t25Tbl">
      <thead><tr id="t25Hdr"></tr></thead>
      <tbody id="t25Body"></tbody>
    </table>
  </div>
</div>

<div class="panel hidden" id="panel-underresults">
  <div class="results-summary" id="underSummary"></div>
  <h3 class="section-title">Daily Hit Rate (since tracking began)</h3>
  <div class="cal-grid" id="underCalGrid"></div>
  <div class="legend" style="margin-top:16px;">
    <span class="green">Green ✓ = won (UD UNDER call correct)</span>
    <span class="red">Red ✗ = lost</span>
    <span class="yellow">Yellow ~ = push (landed exactly on the line)</span>
    <span style="background:#1c2944;color:#9fb0cc;border:1px solid #555;">Gray DNP = didn't play, excluded from the record</span>
  </div>
  <h3 class="section-title" id="underYesterdayTitle">Latest Graded Day's Band Plays</h3>
  <div class="card-grid" id="underCardGrid"></div>
</div>



<script>
const UD_UNDER_BAND_LABEL = {json.dumps(ud_under_band_label())};
const CARDS = {cards_js};
const VALUE_CARDS = {value_cards_js};
const UNANCHORED_CARDS = {unanchored_cards_js};
const UNDERS_CARDS = {unders_cards_js};
const UD_UNDER_BAND_HEADER = {json.dumps(ud_under_band_header())};
const UNDERS_BAND_COUNT = {unders_band_count};
const UNDERS_TOTAL_COUNT = {unders_total_count};
const COLS = {cols_js};
const ROWS = {rows_js};
const TEAMS = {teams_js};
const CAL = {calendar_js};
const PLAYER_HISTORY = {player_history_js};
const T25_CARDS = {yesterday_cards_js};
const T25_DAILY_DATE = {json.dumps(daily_date)};
const T25_DAILY_RATE = {json.dumps(daily_hit_rate)};
const T25_ROLLING_RATE = {json.dumps(rolling_hit_rate)};
const FIRE_CAL = {fire_cal_js};
const HOT_CAL = {hot_cal_js};
const FIRE_SUMMARY = {fire_summary_js};
const HOT_SUMMARY = {hot_summary_js};
const T25_BEST = {json.dumps(best_performer)};
const T25_WORST = {json.dumps(worst_performer)};
const T25_RECORDS = {record_rows_js};
const T25_CAL = {t25_calendar_js};
const T25_ALL_TIME_RATE = {json.dumps(t25_all_time_rate)};
const T25_ALL_TIME_N = {t25_all_decided};
const T25_ALL_TIME_HITS = {t25_all_hits};
const UNDER_CAL = {under_calendar_js};
const UNDER_YESTERDAY_DATE = {json.dumps(under_yesterday_date)};
const UNDER_YESTERDAY_CARDS = {under_yesterday_cards_js};
const UNDER_TOTAL_WINS = {under_total_wins};
const UNDER_TOTAL_LOSSES = {under_total_losses};
const UNDER_TOTAL_N = {under_total_n};
const UNDER_TOTAL_RATE = {json.dumps(under_total_rate)};
const UNDER_TOTAL_DNP = {under_total_dnp};
const UNDER_CI_LO = {json.dumps(under_ci_lo)};
const UNDER_CI_HI = {json.dumps(under_ci_hi)};
const GENERATED_AT = {json.dumps(generated_at_iso)};
const GAME_DATE = {json.dumps(date_str)};
const PLAYER_COUNT = {player_count};
const GAMES_COUNT = {games_count};

// --- Freshness indicator ---
function updateFreshness() {{
  const generated = new Date(GENERATED_AT);
  const now = new Date();
  const ageHours = (now - generated) / 3600000;

  const timeStr = generated.toLocaleString('en-US', {{
    month: 'long', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true,
  }});
  const lastUpdatedEl = document.getElementById('lastUpdated');
  const todayStr = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');
  // A recent rebuild timestamp does not imply fresh game data — the pipeline
  // can regenerate the dashboard from yesterday's data file if today's fetch
  // failed silently. "Fresh" requires BOTH a recent rebuild AND GAME_DATE
  // matching today, not just ageHours.
  if (ageHours > 4) {{
    lastUpdatedEl.innerHTML = `Last Updated: ${{timeStr}} PT &mdash; <span style="color:#f87171">&#9888; Data may be stale - pipeline may not have run</span>`;
  }} else if (GAME_DATE !== todayStr) {{
    lastUpdatedEl.innerHTML = `Last Updated: ${{timeStr}} PT &mdash; <span style="color:#f87171">&#9888; Rebuilt recently but showing ${{GAME_DATE}}'s data, not today's</span>`;
  }} else {{
    lastUpdatedEl.innerHTML = `Last Updated: ${{timeStr}} PT &mdash; <span style="color:#4ade80">&#10003; Fresh</span>`;
  }}

  const banner = document.getElementById('freshnessBanner');
  const updatedTime = generated.toLocaleTimeString('en-US', {{ hour: 'numeric', minute: '2-digit', hour12: true }});
  if (GAME_DATE === todayStr) {{
    banner.className = 'freshness-banner fresh';
    banner.innerHTML = `&#10003; Today's data &mdash; ${{GAME_DATE}} &mdash; ${{PLAYER_COUNT}} players &mdash; ${{GAMES_COUNT}} games &mdash; Updated ${{updatedTime}}`;
  }} else {{
    banner.className = 'freshness-banner stale';
    banner.innerHTML = `&#9888; Showing data from ${{GAME_DATE}} &mdash; pipeline may not have run today`;
  }}
}}
updateFreshness();
setInterval(updateFreshness, 60000);
setInterval(() => location.reload(), 1800000);

// --- Top 25 / Value Play cards ---
function edgeRowHtml(c) {{
  if (c.edgeLabel === null || c.edgeLabel === undefined) return '';
  const sign = c.edge > 0 ? '+' : '';
  if (c.edgeLabel === 'over') {{
    return `<div class="edge-row"><span class="edge-tag over">&#8593; OVER</span> ${{sign}}${{c.edge.toFixed(2)}} vs line ${{c.udLine.toFixed(1)}}</div>`;
  }}
  if (c.edgeLabel === 'under') {{
    return `<div class="edge-row"><span class="edge-tag under">&#8595; UNDER</span> ${{sign}}${{c.edge.toFixed(2)}} vs line ${{c.udLine.toFixed(1)}}</div>`;
  }}
  return `<div class="edge-row"><span class="edge-tag neutral">NEUTRAL</span> ${{sign}}${{c.edge.toFixed(2)}} vs line ${{c.udLine.toFixed(1)}}</div>`;
}}

function platoonMatchupHtml(c) {{
  const pm = c.platoonMatchup;
  if (!pm) return '';
  const cls = pm.advantage === 'batter' ? 'edge-batter' : (pm.advantage === 'pitcher' ? 'edge-pitcher' : 'edge-neutral');
  const arrow = pm.advantage === 'batter' ? '&#9650; Batter edge' : (pm.advantage === 'pitcher' ? '&#9660; Pitcher edge' : '&#9644; Neutral');
  const bWoba = pm.batterWoba != null ? pm.batterWoba.toFixed(3) : 'N/A';
  const pWoba = pm.pitcherWoba != null ? pm.pitcherWoba.toFixed(3) : 'N/A';
  return `
    <div class="platoon-matchup ${{cls}}">
      <div class="platoon-title">Platoon Matchup &middot; ${{arrow}}</div>
      <div class="platoon-row">
        <span>Batter ${{pm.batterLabel || ''}}: <b>${{bWoba}}</b> wOBA</span>
        <span>Pitcher ${{pm.pitcherLabel || ''}}: <b>${{pWoba}}</b> wOBA-against</span>
      </div>
    </div>`;
}}

function top25RecordHtml(c) {{
  if (!c.top25Record) return '';
  const hasRate = c.top25Record.n >= {TOP25_RECORD_MIN_N_FOR_RATE};
  return `<div class="top25-record${{hasRate ? ' has-rate' : ''}}">${{c.top25Record.text}}</div>`;
}}

// n-gated so a thin sample never gets the same visual weight as a proven
// one: under 8 appearances gets no treatment no matter how hot the rate
// is; 8-14 gets a plain solid border; 15+ gets the animated glow.
function top25TreatmentClass(c) {{
  const r = c.top25Record;
  if (!r || r.n < 8) return '';
  const rate = r.wins / r.n;
  if (rate < 0.55) return '';
  return r.n >= 15 ? 'top25-fire' : 'top25-hot';
}}

function tierBadgesHtml(c) {{
  return (c.valuePlay ? '<div class="badge badge-value">VALUE PLAY</div>' : '')
    + (c.udUnderBand ? `<div class="badge badge-under-band" title="Raw edge bucket, not a tier - tracked separately since {UD_UNDER_BAND_SEED_END}">${{UD_UNDER_BAND_LABEL}}</div>` : '');
}}

// --- Click-to-mark-used: a card you've already acted on dims out so your
// eye skips it on the next pass. Purely visual (nothing reorders/hides),
// shared across every grid a player's card appears in (Top 25/Unders/
// Value/Unanchored), and keyed by today's date so it resets on its own
// tomorrow rather than needing an explicit expiry. -----------------------
const usedStorageKey = 'mlbUsedCards_' + GAME_DATE;
let usedIds = new Set();
try {{
  usedIds = new Set(JSON.parse(localStorage.getItem(usedStorageKey) || '[]'));
}} catch (e) {{}}

function saveUsedIds() {{
  try {{ localStorage.setItem(usedStorageKey, JSON.stringify([...usedIds])); }} catch (e) {{}}
}}

function applyUsedStateToAllCards() {{
  document.querySelectorAll('.card[data-player-id]').forEach(el => {{
    el.classList.toggle('used', usedIds.has(Number(el.dataset.playerId)));
  }});
}}

function toggleUsed(playerId) {{
  if (usedIds.has(playerId)) {{ usedIds.delete(playerId); }} else {{ usedIds.add(playerId); }}
  saveUsedIds();
  applyUsedStateToAllCards();
}}

document.querySelectorAll('.clear-marks-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    usedIds.clear();
    saveUsedIds();
    applyUsedStateToAllCards();
  }});
}});

function renderCard(c, treatmentFn) {{
  const card = document.createElement('div');
  const treatment = treatmentFn ? treatmentFn(c) : top25TreatmentClass(c);
  card.className = ('card ' + c.tier + ' ' + treatment).trim();
  if (c.gameDateUtc) card.dataset.gameTimeUtc = c.gameDateUtc;
  if (c.playerId !== null && c.playerId !== undefined) {{
    card.dataset.playerId = c.playerId;
    if (usedIds.has(c.playerId)) card.classList.add('used');
    card.addEventListener('click', () => toggleUsed(c.playerId));
  }}
  card.innerHTML = `
    <div class="name">${{c.name}}</div>
    <div class="meta">${{c.team}} &middot; Batting ${{c.order}} &middot; <span class="game-date">${{GAME_DATE}}</span>${{c.gameTimePt ? ` &middot; <span class="game-time">${{c.gameTimePt}}</span>` : ''}}</div>
    <div class="pts-row">
      <div><span class="ud-pts">${{c.ud}}</span><span class="pts-label">UD PTS</span></div>
      <div><span class="pp-pts">${{c.pp}}</span><span class="pts-label">PP PTS</span></div>
    </div>
    <div class="stat-line">xwOBA ${{c.xwoba}} &nbsp;|&nbsp; Barrel% ${{c.barrel}} &nbsp;|&nbsp; Opp ERA ${{c.era}}</div>
    <div class="stat-line">${{c.wxIcon}} ${{c.wxText}} &nbsp;|&nbsp; Park ${{c.park}}</div>
    ${{top25RecordHtml(c)}}
    ${{edgeRowHtml(c)}}
    ${{platoonMatchupHtml(c)}}
    ${{c.platoon ? '<div class="badge">Platoon Edge</div>' : ''}}
    ${{c.adjusted ? '<div class="badge badge-adjusted">Model adjusted</div>' : ''}}
    ${{c.anchored ? '<div class="badge badge-anchored">Live line</div>' : ''}}
    ${{c.projectedLineup && !c.getawayDayRisk ? '<div class="badge badge-projected">&#9888; Projected Lineup</div>' : ''}}
    ${{c.noLinePenalty ? '<div class="badge badge-no-line">&#9888; No Line &ndash; Lower Confidence</div>' : ''}}
    ${{c.getawayDayRisk ? '<div class="badge badge-getaway">&#9888; Projected Lineup &ndash; Getaway Day Risk</div>' : ''}}
    ${{tierBadgesHtml(c)}}
  `;
  return card;
}}

const grid = document.getElementById('cardGrid');
for (const c of CARDS) {{
  grid.appendChild(renderCard(c));
}}

// --- Value Plays ---
const valueGrid = document.getElementById('valueGrid');
if (VALUE_CARDS.length === 0) {{
  valueGrid.outerHTML = '<div class="value-plays-empty">No 1.5+ pt disagreements with the market right now.</div>';
}} else {{
  for (const c of VALUE_CARDS) {{
    valueGrid.appendChild(renderCard(c));
  }}
}}

// --- Unanchored (no posted line) ---
const unanchoredGrid = document.getElementById('unanchoredGrid');
if (UNANCHORED_CARDS.length === 0) {{
  unanchoredGrid.outerHTML = '<div class="unanchored-empty">All Top 25 players have a posted UD/PP line.</div>';
}} else {{
  for (const c of UNANCHORED_CARDS) {{
    unanchoredGrid.appendChild(renderCard(c));
  }}
}}

// --- Unders tab: every UD UNDER call, sorted biggest-edge-first server
// side. Cards in the validated 1.5-2.0 band get the same fire glow as a
// hot Top 25 streak (top25-fire) - everything else renders like a normal
// card, same as Top 25/Value/Unanchored. ------------------------------
document.getElementById('undersHeader').innerHTML = `
  <div class="unders-band-line">${{UD_UNDER_BAND_HEADER}}</div>
  <div class="unders-count-line">${{UNDERS_BAND_COUNT}} of ${{UNDERS_TOTAL_COUNT}} UNDER calls today are in the band</div>
`;
const undersGrid = document.getElementById('undersGrid');
if (UNDERS_CARDS.length === 0) {{
  undersGrid.outerHTML = '<div class="unanchored-empty">No UD UNDER calls today.</div>';
}} else {{
  for (const c of UNDERS_CARDS) {{
    undersGrid.appendChild(renderCard(c, cc => cc.udUnderBand ? 'top25-fire' : ''));
  }}
}}

// --- Hide-started-games filter (applies to Top 25, Full Leaderboard,
// Value Plays, Unanchored - NOT Results/Player History, which are
// historical). Single shared toggle so state stays consistent across tabs.
let hideStarted = false;

function gameHasStarted(iso) {{
  if (!iso) return false;
  return new Date(iso).getTime() <= Date.now();
}}

function applyHideStartedFilter() {{
  document.querySelectorAll('[data-game-time-utc]').forEach(el => {{
    el.style.display = (hideStarted && gameHasStarted(el.dataset.gameTimeUtc)) ? 'none' : '';
  }});
}}

const hideStartedToggle = document.getElementById('hideStartedToggle');
hideStartedToggle.addEventListener('change', () => {{
  hideStarted = hideStartedToggle.checked;
  applyHideStartedFilter();
  render(); // re-apply to the Full Leaderboard table too
}});

applyHideStartedFilter();
setInterval(applyHideStartedFilter, 30000); // live re-check as games start, no reload needed


// --- Tabs ---
const PANELS = ['top25', 'unders', 'full', 'results', 'history', 'top25results', 'underresults'];
document.querySelectorAll('.tab-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    for (const name of PANELS) {{
      document.getElementById('panel-' + name).classList.toggle('hidden', btn.dataset.tab !== name);
    }}
  }});
}});

// --- Results: calendar heatmap ---
function hitColor(rate) {{
  if (rate >= 50) return '#4ade80';
  if (rate >= 30) return '#fbbf24';
  return '#f87171';
}}

const calGrid = document.getElementById('calGrid');
for (const c of CAL) {{
  const cell = document.createElement('div');
  cell.className = 'cal-cell' + (c.lowCoverage ? ' low-coverage' : '');
  cell.style.borderColor = hitColor(c.ud_hit_rate);
  cell.title = `${{c.date}}\\nUD hit rate: ${{c.ud_hit_rate}}%\\nPP hit rate: ${{c.pp_hit_rate}}%\\nPlayers graded: ${{c.player_count}}`
    + (c.lowCoverage ? `\\n\\n⚠ LOW COVERAGE: ${{c.lowCoverageReason}} - grading this day is unrepresentative.` : '');
  cell.innerHTML = `<div class="cal-date">${{c.date.slice(5)}}</div><div class="cal-rate">${{c.ud_hit_rate}}%</div>`;
  calGrid.appendChild(cell);
}}
if (CAL.length === 0) {{
  calGrid.innerHTML = '<div class="empty-msg">No results yet — run tracker.py after games finish.</div>';
}}

// --- Player History ---
const phSearch = document.getElementById('phSearch');
const phResults = document.getElementById('phResults');

function renderHistory() {{
  const q = phSearch.value.trim().toLowerCase();
  phResults.innerHTML = '';
  if (!q) return;
  const matches = Object.values(PLAYER_HISTORY).filter(p => p.name.toLowerCase().includes(q)).slice(0, 10);
  if (matches.length === 0) {{
    phResults.innerHTML = '<div class="empty-msg">No history found for that player.</div>';
    return;
  }}
  for (const p of matches) {{
    const div = document.createElement('div');
    div.className = 'ph-player';
    const rowsHtml = p.history.slice().reverse().map(h => `
      <tr>
        <td>${{h.date}}${{h.game_time_pt ? ` <span class="meta">${{h.game_time_pt}}</span>` : ''}}</td>
        <td>${{h.projected_ud != null ? h.projected_ud.toFixed(1) : 'N/A'}}</td>
        <td>${{h.ud_line != null ? h.ud_line.toFixed(1) : '—'}}</td>
        <td>${{h.actual_ud}}</td>
        <td class="res-${{h.result_ud || 'none'}}">${{h.result_ud || '—'}}</td>
        <td>${{h.projected_pp != null ? h.projected_pp.toFixed(1) : 'N/A'}}</td>
        <td>${{h.pp_line != null ? h.pp_line.toFixed(1) : '—'}}</td>
        <td>${{h.actual_pp}}</td>
        <td class="res-${{h.result_pp || 'none'}}">${{h.result_pp || '—'}}</td>
      </tr>`).join('');
    div.innerHTML = `
      <h3>${{p.name}} <span class="meta">${{p.team}}</span></h3>
      <table class="ph-table">
        <thead><tr><th>Date</th><th>Proj UD</th><th>UD Line</th><th>Actual UD</th><th>UD Result</th><th>Proj PP</th><th>PP Line</th><th>Actual PP</th><th>PP Result</th></tr></thead>
        <tbody>${{rowsHtml}}</tbody>
      </table>`;
    phResults.appendChild(div);
  }}
}}

phSearch.addEventListener('input', renderHistory);

// --- Top 25 Results ---
const t25Summary = document.getElementById('t25Summary');
{{
  let html = '';
  if (T25_DAILY_DATE) {{
    const decidedCount = T25_CARDS.filter(c => c.grade === 'win' || c.grade === 'loss').length;
    const hitCount = T25_CARDS.filter(c => c.grade === 'win').length;
    html += `<div class="summary-card"><div class="summary-value">${{hitCount}}/${{decidedCount}}</div>
             <div class="summary-label">${{T25_DAILY_DATE}}: wins (${{T25_DAILY_RATE !== null ? T25_DAILY_RATE : 'N/A'}}%)</div></div>`;
  }} else {{
    html += `<div class="summary-card"><div class="summary-value">N/A</div>
             <div class="summary-label">No Top 25 results yet</div></div>`;
  }}
  html += `<div class="summary-card"><div class="summary-value">${{T25_ROLLING_RATE !== null ? T25_ROLLING_RATE + '%' : 'N/A'}}</div>
           <div class="summary-label">Last 7 days hit rate</div></div>`;
  if (T25_BEST) {{
    html += `<div class="summary-card best"><div class="summary-value">${{T25_BEST.rate}}%</div>
             <div class="summary-label">Best this week: ${{T25_BEST.name}}</div></div>`;
  }}
  if (T25_WORST) {{
    html += `<div class="summary-card worst"><div class="summary-value">${{T25_WORST.rate}}%</div>
             <div class="summary-label">Worst this week: ${{T25_WORST.name}}</div></div>`;
  }}
  html += `<div class="summary-card"><div class="summary-value">${{T25_ALL_TIME_RATE !== null ? T25_ALL_TIME_RATE + '%' : 'N/A'}}</div>
           <div class="summary-label">All-time (${{T25_ALL_TIME_HITS}}/${{T25_ALL_TIME_N}}, ${{T25_CAL.length}} days tracked)</div></div>`;
  t25Summary.innerHTML = html;
}}

const t25CalGrid = document.getElementById('t25CalGrid');
for (const c of T25_CAL) {{
  const cell = document.createElement('div');
  cell.className = 'cal-cell';
  cell.style.borderColor = c.hit_rate === null ? '#555' : hitColor(c.hit_rate);
  cell.title = `${{c.date}}\\n${{c.hit_rate !== null ? c.hit_rate + '% (' + c.n + ' decided)' : 'No decided calls'}}`;
  cell.innerHTML = `<div class="cal-date">${{c.date.slice(5)}}</div><div class="cal-rate">${{c.hit_rate !== null ? c.hit_rate + '%' : '—'}}</div>`;
  t25CalGrid.appendChild(cell);
}}
if (T25_CAL.length === 0) {{
  t25CalGrid.innerHTML = '<div class="empty-msg">No Top 25 results yet — run tracker.py after games finish.</div>';
}}

const t25Grid = document.getElementById('t25CardGrid');
for (const c of T25_CARDS) {{
  const card = document.createElement('div');
  let borderClass = 'nodata';
  let overlay = '';
  if (c.grade === 'win') {{
    borderClass = 'win';
    overlay = '<div class="t25-overlay win">&#10003;</div>';
  }} else if (c.grade === 'push') {{
    borderClass = 'push';
    overlay = '<div class="t25-overlay push">~</div>';
  }} else if (c.grade === 'loss') {{
    borderClass = 'loss';
    overlay = '<div class="t25-overlay loss">&#10007;</div>';
  }}
  let callBadge = '';
  if (c.ud_line != null && c.projected_ud != null) {{
    if (c.projected_ud > c.ud_line) {{
      callBadge = '<div class="t25-call over">&#8593; OVER</div>';
    }} else if (c.projected_ud < c.ud_line) {{
      callBadge = '<div class="t25-call under">&#8595; UNDER</div>';
    }}
  }}
  card.className = 'card t25-card ' + borderClass;
  card.innerHTML = `
    ${{overlay}}
    <div class="name">${{c.name}}</div>
    <div class="meta">${{c.team}} &middot; Batting ${{c.order}} &middot; <span class="game-date">${{c.date}}</span>${{c.gameTimePt ? ` &middot; <span class="game-time">${{c.gameTimePt}}</span>` : ''}}</div>
    <div class="pts-row">
      <div><span class="ud-pts">${{c.ud}}</span><span class="pts-label">PROJ UD</span></div>
      <div><span class="line-pts">${{c.ud_line != null ? c.ud_line.toFixed(1) : 'N/A'}}</span><span class="pts-label">UD LINE</span></div>
      <div><span class="pp-pts">${{c.actual_ud !== null ? c.actual_ud : 'N/A'}}</span><span class="pts-label">ACTUAL UD</span></div>
    </div>
    ${{callBadge}}
    <div class="stat-line">xwOBA ${{c.xwoba}} &nbsp;|&nbsp; Barrel% ${{c.barrel}} &nbsp;|&nbsp; Opp ERA ${{c.era}}</div>
    <div class="stat-line">${{c.wxIcon}} ${{c.wxText}} &nbsp;|&nbsp; Park ${{c.park}}</div>
    ${{top25RecordHtml(c)}}
    ${{c.platoon ? '<div class="badge">Platoon Edge</div>' : ''}}
    ${{c.adjusted ? '<div class="badge badge-adjusted">Model adjusted</div>' : ''}}
    ${{c.noLinePenalty ? '<div class="badge badge-no-line">&#9888; No Line &ndash; Lower Confidence</div>' : ''}}
    ${{c.getawayDayRisk ? '<div class="badge badge-getaway">&#9888; Projected Lineup &ndash; Getaway Day Risk</div>' : ''}}
    ${{tierBadgesHtml(c)}}
  `;
  t25Grid.appendChild(card);
}}
if (T25_CARDS.length === 0) {{
  t25Grid.innerHTML = '<div class="empty-msg">No Top 25 results yet — run tracker.py after games finish.</div>';
}}

// --- Fire/Hot Marked Plays cohorts (UNVALIDATED - see
// tracker.track_marked_plays). Renders a summary card + a daily calendar
// strip per cohort, same "W-L (rate%)" tile format as Under Results so a
// percentage never appears without the record behind it. -----------------
function renderMarkCohort(summaryElId, calElId, summary, cal, emptyMsg) {{
  const summaryEl = document.getElementById(summaryElId);
  const ciText = summary.n ? `${{summary.ci_lo}}&ndash;${{summary.ci_hi}}%` : 'N/A';
  summaryEl.innerHTML = `
    <div class="summary-card"><div class="summary-value">${{summary.wins}}-${{summary.losses}}${{summary.rate !== null ? ' (' + summary.rate + '%)' : ''}}</div>
         <div class="summary-label">All-time record (n=${{summary.n}})</div></div>
    <div class="summary-card"><div class="summary-value">${{ciText}}</div>
         <div class="summary-label">95% Wilson CI</div></div>
  `;
  const calEl = document.getElementById(calElId);
  for (const c of cal) {{
    if (c.n === 0) continue;
    const cell = document.createElement('div');
    cell.className = 'cal-cell wide';
    cell.style.borderColor = c.rate === null ? '#555' : hitColor(c.rate);
    cell.title = `${{c.date}}\\n${{c.wins}}-${{c.losses}}${{c.rate !== null ? ' (' + c.rate + '%)' : ''}}`;
    cell.innerHTML = `<div class="cal-date">${{c.date.slice(5)}}</div><div class="cal-rate">${{c.wins}}-${{c.losses}} (${{c.rate}}%)</div>`;
    calEl.appendChild(cell);
  }}
  if (!cal.some(c => c.n > 0)) {{
    calEl.innerHTML = `<div class="empty-msg">${{emptyMsg}}</div>`;
  }}
}}
renderMarkCohort('fireSummary', 'fireCalGrid', FIRE_SUMMARY, FIRE_CAL,
  'No fire-marked plays yet - needs a player with 15+ Top 25 appearances at a 55%+ historical rate.');
renderMarkCohort('hotSummary', 'hotCalGrid', HOT_SUMMARY, HOT_CAL,
  'No hot-marked plays yet - needs a player with 8-14 Top 25 appearances at a 55%+ historical rate.');

// --- Under Results (UD UNDER 1.5-2.0 edge band tracking) ----------------
const underSummary = document.getElementById('underSummary');
{{
  let html = '';
  html += `<div class="summary-card"><div class="summary-value">${{UNDER_TOTAL_WINS}}-${{UNDER_TOTAL_LOSSES}} (${{UNDER_TOTAL_RATE}}%)</div>
           <div class="summary-label">All-time band record (n=${{UNDER_TOTAL_N}})</div></div>`;
  html += `<div class="summary-card"><div class="summary-value">${{UNDER_CI_LO}}&ndash;${{UNDER_CI_HI}}%</div>
           <div class="summary-label">95% Wilson CI</div></div>`;
  html += `<div class="summary-card"><div class="summary-value">${{UNDER_TOTAL_DNP}}</div>
           <div class="summary-label">DNP (excluded from record)</div></div>`;
  underSummary.innerHTML = html;
}}

const underCalGrid = document.getElementById('underCalGrid');
for (const c of UNDER_CAL) {{
  const cell = document.createElement('div');
  cell.className = 'cal-cell wide' + (c.lowCoverage ? ' low-coverage' : '');
  cell.style.borderColor = c.lowCoverage ? '#fb923c' : (c.hit_rate === null ? '#555' : hitColor(c.hit_rate));
  cell.title = `${{c.date}}\\n${{c.wins}}-${{c.losses}}${{c.hit_rate !== null ? ' (' + c.hit_rate + '%)' : ''}}\\nDNP: ${{c.dnp}}`
    + (c.lowCoverage ? `\\n\\n⚠ LOW COVERAGE: ${{c.lowCoverageReason}} - excluded from the band's all-time record/rate.` : '');
  cell.innerHTML = `<div class="cal-date">${{c.date.slice(5)}}</div><div class="cal-rate">${{c.hit_rate !== null ? c.wins + '-' + c.losses + ' (' + c.hit_rate + '%)' : (c.lowCoverage ? 'excl.' : '—')}}</div>`;
  underCalGrid.appendChild(cell);
}}
if (UNDER_CAL.length === 0) {{
  underCalGrid.innerHTML = '<div class="empty-msg">No band results yet — tracking starts the day after ' + {json.dumps(UD_UNDER_BAND_SEED_END)} + '.</div>';
}}

document.getElementById('underYesterdayTitle').textContent = UNDER_YESTERDAY_DATE
  ? `${{UNDER_YESTERDAY_DATE}}'s Band Plays` : 'Latest Graded Day\\'s Band Plays';

const underGrid = document.getElementById('underCardGrid');
for (const c of UNDER_YESTERDAY_CARDS) {{
  // Plays graded before this schema existed only have a boolean "win" -
  // fall back to deriving grade from that so old dates don't misrender
  // as DNP.
  const grade = c.grade || (c.win === true ? 'win' : c.win === false ? 'loss' : 'dnp');
  const card = document.createElement('div');
  let borderClass = 'nodata';
  let overlay = '<div class="t25-overlay dnp">DNP</div>';
  if (grade === 'win') {{
    borderClass = 'win';
    overlay = '<div class="t25-overlay win">&#10003;</div>';
  }} else if (grade === 'loss') {{
    borderClass = 'loss';
    overlay = '<div class="t25-overlay loss">&#10007;</div>';
  }} else if (grade === 'push') {{
    borderClass = 'push';
    overlay = '<div class="t25-overlay push">~</div>';
  }}
  card.className = 'card t25-card ' + borderClass;
  card.innerHTML = `
    ${{overlay}}
    <div class="name">${{c.name}}</div>
    <div class="meta">${{c.team || ''}}${{c.game_time_pt ? ` &middot; <span class="game-time">${{c.game_time_pt}}</span>` : ''}}</div>
    <div class="pts-row">
      <div><span class="ud-pts">${{c.projected_ud != null ? c.projected_ud.toFixed(1) : 'N/A'}}</span><span class="pts-label">PROJ UD</span></div>
      <div><span class="line-pts">${{c.ud_line != null ? c.ud_line.toFixed(1) : 'N/A'}}</span><span class="pts-label">UD LINE</span></div>
      <div><span class="pp-pts">${{c.actual_ud != null ? c.actual_ud : (grade === 'dnp' ? 'DNP' : 'N/A')}}</span><span class="pts-label">ACTUAL UD</span></div>
    </div>
    <div class="edge-row"><span class="edge-tag under">&#8595; UNDER</span> ${{c.edge != null ? c.edge.toFixed(2) : ''}} vs line ${{c.ud_line != null ? c.ud_line.toFixed(1) : ''}}</div>
  `;
  underGrid.appendChild(card);
}}
if (UNDER_YESTERDAY_CARDS.length === 0) {{
  underGrid.innerHTML = '<div class="empty-msg">No band plays graded yet.</div>';
}}

const T25_COLS = [
  {{key: 'name',       label: 'Player'}},
  {{key: 'times',      label: 'Times in Top 25'}},
  {{key: 'record',     label: 'Record'}},
  {{key: 'hit_rate',   label: 'Hit Rate %'}},
  {{key: 'avg_proj',   label: 'Avg Projected'}},
  {{key: 'avg_actual', label: 'Avg Actual'}},
  {{key: 'trend',      label: 'Trend (last 5)'}},
];
const t25Hdr = document.getElementById('t25Hdr');
T25_COLS.forEach((c, i) => {{
  const th = document.createElement('th');
  th.textContent = c.label;
  th.addEventListener('click', () => sortT25(i));
  t25Hdr.appendChild(th);
}});

let t25SortCol = 3; // Hit Rate %
let t25SortDir = -1;

function sortT25(i) {{
  if (t25SortCol === i) {{
    t25SortDir *= -1;
  }} else {{
    t25SortCol = i;
    t25SortDir = -1;
  }}
  renderT25();
}}

function recordRowClass(rate) {{
  if (rate >= 60) return 'row-green';
  if (rate >= 40) return 'row-yellow';
  return 'row-red';
}}

function renderT25() {{
  const key = T25_COLS[t25SortCol].key;
  const sorted = T25_RECORDS.slice().sort((a, b) => {{
    const av = a[key], bv = b[key];
    let cmp;
    if (typeof av === 'number' && typeof bv === 'number') {{
      cmp = av - bv;
    }} else {{
      cmp = String(av).localeCompare(String(bv));
    }}
    return cmp * t25SortDir;
  }});

  Array.from(t25Hdr.children).forEach((th, i) => {{
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (i === t25SortCol) th.classList.add(t25SortDir === 1 ? 'sorted-asc' : 'sorted-desc');
  }});

  const body = document.getElementById('t25Body');
  body.innerHTML = '';
  for (const r of sorted) {{
    const tr = document.createElement('tr');
    tr.className = recordRowClass(r.hit_rate);
    for (const c of T25_COLS) {{
      const td = document.createElement('td');
      td.textContent = c.key === 'hit_rate' ? r[c.key] + '%' : r[c.key];
      tr.appendChild(td);
    }}
    body.appendChild(tr);
  }}
  if (sorted.length === 0) {{
    body.innerHTML = '<tr><td class="empty-msg">No graded Top 25 history yet.</td></tr>';
  }}
}}

renderT25();

// --- Full leaderboard table ---
const hdr = document.getElementById('hdr');
COLS.forEach((c, i) => {{
  const th = document.createElement('th');
  th.textContent = c.label;
  th.dataset.idx = i;
  th.addEventListener('click', () => sortBy(i));
  hdr.appendChild(th);
}});

const teamFilter = document.getElementById('teamFilter');
for (const t of TEAMS) {{
  const opt = document.createElement('option');
  opt.value = t;
  opt.textContent = t;
  teamFilter.appendChild(opt);
}}

const GAME_TIME_COL_IDX = COLS.findIndex(c => c.key === 'game_time_pt');
let sortCol = GAME_TIME_COL_IDX >= 0 ? GAME_TIME_COL_IDX : 4;
let sortDir = 1; // ascending = soonest game first, by default

function sortBy(i) {{
  if (sortCol === i) {{
    sortDir *= -1;
  }} else {{
    sortCol = i;
    sortDir = i === GAME_TIME_COL_IDX ? 1 : -1;
  }}
  render();
}}

function render() {{
  const filter = document.getElementById('search').value.trim().toLowerCase();
  const team = teamFilter.value;
  let rows = ROWS.filter(r => String(r.cells[1]).toLowerCase().includes(filter));
  if (team) rows = rows.filter(r => r.team === team);
  if (hideStarted) rows = rows.filter(r => !gameHasStarted(r.gameDateUtc));

  rows = rows.slice().sort((a, b) => {{
    if (sortCol === GAME_TIME_COL_IDX) {{
      return ((a.gameDateUtc || '9999').localeCompare(b.gameDateUtc || '9999')) * sortDir;
    }}
    const av = a.cells[sortCol], bv = b.cells[sortCol];
    const an = parseFloat(av), bn = parseFloat(bv);
    let cmp;
    if (!isNaN(an) && !isNaN(bn)) {{
      cmp = an - bn;
    }} else {{
      cmp = String(av).localeCompare(String(bv));
    }}
    return cmp * sortDir;
  }});

  Array.from(hdr.children).forEach((th, i) => {{
    th.classList.remove('sorted-asc', 'sorted-desc');
    if (i === sortCol) th.classList.add(sortDir === 1 ? 'sorted-asc' : 'sorted-desc');
  }});

  const body = document.getElementById('body');
  body.innerHTML = '';
  for (const r of rows) {{
    const tr = document.createElement('tr');
    if (r.color) tr.classList.add(r.color);
    if (r.underBand) tr.classList.add('under-band-row');
    r.cells.forEach((c, i) => {{
      const td = document.createElement('td');
      td.textContent = c;
      if (i === 0 && r.underBand) td.title = UD_UNDER_BAND_LABEL;
      tr.appendChild(td);
    }});
    body.appendChild(tr);
  }}
}}

document.getElementById('search').addEventListener('input', render);
teamFilter.addEventListener('change', render);
render();
</script>
</body>
</html>
"""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def prepare_dashboard_context(date_arg=None):
    """Load and process the latest projection data, returning everything
    needed to render the dashboard: rows, the date they're for, and the
    results/top25 history used by the Results / Player History tabs."""
    players, date_str = proj.load_data(date_arg)

    results_path = os.path.join("data", "results", "all_results.json")
    results_data = None
    if os.path.exists(results_path):
        with open(results_path, encoding="utf-8") as f:
            results_data = json.load(f)

    rows = [build_row(p) for p in players]
    recalibrate_points(rows)
    apply_venue_penalty(rows)

    market_lines = get_market_lines(date_str)
    market_corrections = (results_data or {}).get("market_corrections", {})
    anchored_ids = apply_market_anchor(rows, market_lines, market_corrections)

    # Daily Betr snapshot only - Stacks (and its shadow-mode mixed-market
    # research path, stacks_mixed_market.py) was retired 2026-08-18; see
    # tracker.py. Betr line collection stays on regardless: it's cheap and
    # get_betr_lines() caches the raw snapshot to disk on its own, so this
    # keeps building real day-by-day Betr coverage history for any future
    # correlation research even with nothing consuming it live right now.
    try:
        get_betr_lines(date_str)
    except Exception as e:
        print(f"Betr snapshot fetch failed (non-fatal): {e}")

    corrections = build_corrections(results_data)
    apply_corrections(rows, corrections, skip=anchored_ids)
    apply_no_line_penalty(rows, anchored_ids)
    rows.sort(key=lambda r: r["ud_pts"], reverse=True)

    top25_path = os.path.join("data", "results", "top25_results.json")
    top25_data = None
    if os.path.exists(top25_path):
        with open(top25_path, encoding="utf-8") as f:
            top25_data = json.load(f)

    return rows, date_str, results_data, top25_data


def regenerate_dashboard(date_arg=None):
    """Rebuild output/dashboard.html from the latest projection + results
    data and push it to GitHub Pages. Used by tracker.py after nightly
    grading so the Results and Player History tabs update without a full
    pipeline run."""
    rows, date_str, results_data, top25_data = prepare_dashboard_context(date_arg)
    html_path = os.path.join("output", "dashboard.html")
    write_dashboard(rows, date_str, html_path, results_data, top25_data)
    print(f"Dashboard saved -> {os.path.abspath(html_path)}")
    deploy_to_github_pages(html_path, date_str)
    return html_path


def main():
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    rows, date_str, results_data, top25_data = prepare_dashboard_context(date_arg)

    os.makedirs("output", exist_ok=True)
    html_path = os.path.join("output", "dashboard.html")
    write_dashboard(rows, date_str, html_path, results_data, top25_data)
    print(f"Dashboard saved -> {os.path.abspath(html_path)}")

    deploy_to_github_pages(html_path, date_str)

    if not os.environ.get("MLB_HEADLESS"):
        webbrowser.open(f"file:///{os.path.abspath(html_path)}")


# ---------------------------------------------------------------------------
# GitHub Pages auto-deploy
#
# Copies the freshly generated dashboard to docs/index.html and pushes it to
# the mlb-fantasy repo so https://macassvic-cmd.github.io/mlb-fantasy/ stays
# in sync with the latest run. Best-effort: any failure (no network, no git,
# merge conflicts, etc.) is logged and swallowed so it never breaks the
# pipeline.
# ---------------------------------------------------------------------------

DOCS_DASHBOARD_PATH = os.path.join("docs", "index.html")


GIT_SUBPROCESS_TIMEOUT = 60  # seconds - git push has hung indefinitely under
# the S4U scheduled-task context (no interactive desktop to satisfy a
# credential prompt), blocking the whole tracker.py/pipeline.py process for
# hours with nothing to time it out. Every git call here is now bounded.


def deploy_to_github_pages(html_path, date_str):
    try:
        os.makedirs("docs", exist_ok=True)
        shutil.copyfile(html_path, DOCS_DASHBOARD_PATH)

        # In GitHub Actions the workflow's commit step handles all git
        # operations for every file at once. Doing a partial commit here
        # would race with that step and leave data files uncommitted.
        if os.environ.get("GITHUB_ACTIONS"):
            print("GitHub Pages: dashboard copied to docs/ (workflow will commit).")
            return

        repo_root = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(["git", "add", "docs/index.html"], cwd=repo_root, check=True,
                        capture_output=True, text=True, timeout=GIT_SUBPROCESS_TIMEOUT)

        status = subprocess.run(["git", "status", "--porcelain", "docs/index.html"],
                                 cwd=repo_root, check=True, capture_output=True, text=True,
                                 timeout=GIT_SUBPROCESS_TIMEOUT)
        if not status.stdout.strip():
            print("GitHub Pages: no dashboard changes to deploy.")
            return

        subprocess.run(["git", "commit", "-q", "-m", f"Update dashboard for {date_str}"],
                        cwd=repo_root, check=True, capture_output=True, text=True,
                        timeout=GIT_SUBPROCESS_TIMEOUT)
        subprocess.run(["git", "push"], cwd=repo_root, check=True, capture_output=True, text=True,
                        timeout=GIT_SUBPROCESS_TIMEOUT)
        print("GitHub Pages: dashboard deployed -> https://macassvic-cmd.github.io/mlb-fantasy/")
    except Exception as e:
        if isinstance(e, subprocess.TimeoutExpired):
            detail = f"timed out after {GIT_SUBPROCESS_TIMEOUT}s"
        elif isinstance(e, subprocess.CalledProcessError):
            detail = e.stderr
        else:
            detail = str(e)
        print(f"GitHub Pages deploy skipped (non-fatal): {detail}")


if __name__ == "__main__":
    main()
