"""
DNP Score - a 0-100 "will NOT start today" probability for MLB position
players, computed BEFORE the official lineup posts. Once a lineup is
confirmed one way or the other, stale_lines.py's hard boolean detector is
the authoritative signal - this score's real value is the pre-confirmation
window (minutes of lead time, not seconds). Pinch-hitting doesn't matter
for MLB grading, so the target is simply "did not start", not the fuller
bench x zero-PA formula originally floated.

Transparent weighted model for v1 (per the plan: explainable now, switch to
a calibrated ML model once dnp_score_results.json has enough labeled
history to train against - see grade_dnp_scores). Every weight below has an
inline comment explaining the number; none of them are backtested yet, so
treat this as a heuristic ranking tool, not a validated edge, until
calibration data says otherwise.

v1 scores every player in data/{date}.json (today's pipeline output -
confirmed + projected starters). It does NOT yet score players who have a
live Dabble/Betr prop but weren't guessed into a lineup slot by pipeline.py
at all - that requires wiring in the live board via board_loader.py, which
is the natural fast-follow once a board JSON is flowing through
dns_watch.py (see stale_lines.run_poll for the name/team/game resolution
this would reuse).

Usage:
  python dnp_score.py --today                 # score most recent data/{date}.json
  python dnp_score.py --date 2026-09-12
  python dnp_score.py --grade 2026-09-10       # grade a previously-scored date
"""

import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import start_history
from report import _is_early_game
from scrapers.betr import normalize_name
from scrapers.mlb_api import get_game_start_data

SCORES_DIR = os.path.join("data", "dnp_scores")
RESULTS_PATH = os.path.join("data", "results", "dnp_score_results.json")
WATCHLIST_EVENTS_PATH = os.path.join("data", "watchlist", "events.jsonl")

# Soft-signal tiers (rss_news_classifier.SOFT_TIER_PATTERNS) that indicate
# elevated bench/sit risk - "probable" and "nearing-return" mean the
# OPPOSITE (a player working back toward playing), so they're deliberately
# excluded here.
RISK_NEWS_TIERS = {"doubtful", "questionable", "game-time-decision", "day-to-day", "news-mention"}
NEWS_LOOKBACK_HOURS = 48

# --- Weights - each documented individually; total is clamped to 0-100 ---
BASE_SCORE = 3  # residual late-scratch risk even for a totally normal case
NOT_CONFIRMED_WEIGHT = 45  # dominant signal - see module docstring
RECENT_START_RATE_WEIGHT = 20  # scaled by (1 - recent start rate), n>=3 required
VS_HAND_WEIGHT = 15  # scaled by how much lower their vs-hand rate is than their overall recent rate
STREAK_FATIGUE_MAX = 10  # consecutive-starts fatigue, capped
STREAK_FATIGUE_MIN_GAMES = 6  # streak length before fatigue starts counting
CATCHER_REST_BONUS = 8  # extra bump for a catcher on a 4+ game streak
CATCHER_REST_MIN_STREAK = 4
GETAWAY_DAY_BONUS = 7  # only applied on top of NOT_CONFIRMED_WEIGHT, not standalone
NEWS_RISK_BONUS = 12
MIN_GAMES_FOR_RATE_SIGNAL = 3  # below this, a rate is noise - skip the signal entirely


def _load_watchlist_risk_map():
    """{normalized_name: (tier, headline)} for the most recent risk-tier
    watchlist hit within NEWS_LOOKBACK_HOURS - see watchlist_dns.py /
    rss_news_classifier.py. Best-effort: a missing/unparseable file just
    means no news signal, not an error."""
    risk = {}
    if not os.path.exists(WATCHLIST_EVENTS_PATH):
        return risk
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_LOOKBACK_HOURS)
    try:
        with open(WATCHLIST_EVENTS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "watchlist_hit" or event.get("sport") != "mlb":
                    continue
                tier = event.get("tier")
                if tier not in RISK_NEWS_TIERS:
                    continue
                ts = event.get("ts")
                try:
                    ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
                except (ValueError, AttributeError):
                    ts_dt = None
                if ts_dt is None or ts_dt < cutoff:
                    continue
                name = normalize_name(event.get("name", ""))
                if name:
                    risk[name] = (tier, event.get("raw_text", ""))
    except Exception:
        pass
    return risk


def _opp_pitcher_hand(row):
    """Derive today's opposing starter's handedness from bat_side +
    platoon.platoon_same_hand (both already computed by the pipeline) -
    avoids a second lookup for something we already have implicitly."""
    bat_side = row.get("bat_side")
    same_hand = (row.get("platoon") or {}).get("platoon_same_hand")
    if bat_side not in ("L", "R") or same_hand is None:
        return None
    return bat_side if same_hand else ("R" if bat_side == "L" else "L")


# Flat score for a player whose lineup IS posted and who is genuinely NOT
# in it - added 2026-09-13 for the live Dabble board (dabble_adapter.py),
# which can represent this state directly (pipeline.py's data/{date}.json
# never can - it only ever contains the 9 players it guessed INTO a lineup
# slot, so a real "confirmed not starting" case has no representation
# there at all). This is near-certainty, not a heuristic blend - it's
# functionally the same fact stale_lines.py's hard boolean detector would
# flag - so it deliberately bypasses the weighted signals below rather
# than being yet another additive contribution.
CONFIRMED_NOT_STARTING_SCORE = 97


def score_candidate(*, player_id, name, position, start_status, opp_pitcher_hand,
                     is_early_game, history, news_risk):
    """Core scorer - both the pipeline path (score_player, below) and the
    live Dabble board path (dabble_adapter.score_dabble_board) funnel
    through this exact function, so the weights are applied identically
    regardless of where the candidate came from. `start_status` is one of
    "confirmed_starting" | "confirmed_not_starting" | "not_yet_posted".
    Returns (score_0_100, reasons: list[str])."""
    if start_status == "confirmed_not_starting":
        return CONFIRMED_NOT_STARTING_SCORE, ["Lineup posted - CONFIRMED not in the starting lineup"]

    score = BASE_SCORE
    reasons = []

    if start_status == "confirmed_starting":
        reasons.append("Confirmed starting in today's lineup")
    else:  # "not_yet_posted" (or "unknown" - treated the same: no confirmation either way yet)
        score += NOT_CONFIRMED_WEIGHT
        reasons.append("Lineup not yet posted/confirmed")
        if is_early_game:
            score += GETAWAY_DAY_BONUS
            reasons.append("Early game while still unconfirmed - getaway-day risk")

    wins, losses, n = start_history.start_rate(history, player_id, n_games=10)
    recent_rate = wins / n if n else None
    if n >= MIN_GAMES_FOR_RATE_SIGNAL:
        contribution = (1 - recent_rate) * RECENT_START_RATE_WEIGHT
        if contribution >= 1:
            score += contribution
            reasons.append(f"Started only {round(100 * recent_rate)}% of last {n} team games")

    if opp_pitcher_hand and n >= MIN_GAMES_FOR_RATE_SIGNAL:
        hw, hl, hn = start_history.start_rate_vs_hand(history, player_id, opp_pitcher_hand, n_games=15)
        if hn >= MIN_GAMES_FOR_RATE_SIGNAL:
            hand_rate = hw / hn
            diff = (recent_rate or 1.0) - hand_rate
            if diff >= 0.15:
                score += diff * VS_HAND_WEIGHT
                reasons.append(f"Starts only {round(100 * hand_rate)}% of the time vs {opp_pitcher_hand}HP "
                                f"(n={hn}, overall {round(100 * (recent_rate or 0))}%)")

    streak = start_history.consecutive_starts(history, player_id)
    if streak >= STREAK_FATIGUE_MIN_GAMES:
        fatigue = min((streak - (STREAK_FATIGUE_MIN_GAMES - 1)) * 2, STREAK_FATIGUE_MAX)
        score += fatigue
        reasons.append(f"On a {streak}-game start streak")
        if position == "C" and streak >= CATCHER_REST_MIN_STREAK:
            score += CATCHER_REST_BONUS
            reasons.append(f"Catcher on a {streak}-game streak - rest risk elevated")
    elif position == "C" and streak >= CATCHER_REST_MIN_STREAK:
        score += CATCHER_REST_BONUS
        reasons.append(f"Catcher on a {streak}-game streak - rest risk elevated")

    news = news_risk.get(normalize_name(name or ""))
    if news:
        tier, headline = news
        score += NEWS_RISK_BONUS
        snippet = (headline or "")[:80]
        reasons.append(f"Recent news ({tier}): {snippet}" if snippet else f"Recent news tier: {tier}")

    score = max(0, min(100, round(score)))
    if not reasons:
        reasons.append("Confirmed starter, no elevated risk signals")
    return score, reasons


def compute_confidence(*, start_status, recent_n, opp_hand_known, game_resolved):
    """0-100: how much data actually backs this candidate's score, NOT a
    restatement of the score itself - a 90 built on a resolved game,
    posted lineup, and 10 games of history means something different from
    a 90 built on none of that. Same "how much data backs this" spirit as
    report.confidence_score, simplified to the signals score_candidate
    actually uses."""
    confidence = 0
    if game_resolved:
        confidence += 25
    if start_status != "unknown":
        confidence += 25
    if recent_n >= MIN_GAMES_FOR_RATE_SIGNAL:
        confidence += 30
    if opp_hand_known:
        confidence += 20
    return confidence


def score_player(row, history, news_risk):
    """(score_0_100, reasons: list[str]) for one player's raw PIPELINE
    record (a dict from data/{date}.json, NOT report.build_row's
    flattened version - see module docstring). Thin wrapper around
    score_candidate: pipeline.py's data/{date}.json only ever contains
    players it guessed INTO a lineup slot, so "confirmed_not_starting"
    never applies here - only "confirmed_starting" (lineup_confirmed=True)
    or "not_yet_posted" (lineup_confirmed=False, i.e. still projected)."""
    start_status = "confirmed_starting" if row.get("lineup_confirmed", True) else "not_yet_posted"
    return score_candidate(
        player_id=row.get("player_id"),
        name=row.get("name"),
        position=row.get("position"),
        start_status=start_status,
        opp_pitcher_hand=_opp_pitcher_hand(row),
        is_early_game=_is_early_game(row.get("game_date_utc")),
        history=history,
        news_risk=news_risk,
    )


def tier_label(score):
    if score >= 90:
        return "DNP SNIPER"
    if score >= 80:
        return "Very High"
    if score >= 70:
        return "Strong Watch"
    if score >= 60:
        return "Watch"
    return "Ignore"


def score_today(date_str):
    """Score every player in data/{date_str}.json, save a snapshot for
    later grading, and return the ranked list."""
    data_path = os.path.join("data", f"{date_str}.json")
    if not os.path.exists(data_path):
        print(f"dnp_score: no data/{date_str}.json found.")
        return []

    with open(data_path, encoding="utf-8") as f:
        raw_players = json.load(f)

    history = start_history._load(start_history.HISTORY_PATH, {})
    news_risk = _load_watchlist_risk_map()

    scored = []
    for row in raw_players:
        score, reasons = score_player(row, history, news_risk)
        scored.append({
            "player_id": row.get("player_id"),
            "name": row.get("name"),
            "team": row.get("team_name"),
            "opp_team": row.get("opp_team_name"),
            "game_pk": row.get("game_pk"),
            "position": row.get("position"),
            "batting_order": row.get("batting_order"),
            "lineup_status": row.get("lineup_status"),
            "score": score,
            "tier": tier_label(score),
            "reasons": reasons,
        })
    scored.sort(key=lambda r: r["score"], reverse=True)

    os.makedirs(SCORES_DIR, exist_ok=True)
    with open(os.path.join(SCORES_DIR, f"{date_str}.json"), "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "candidates": scored}, f, indent=2)

    return scored


def print_ranked(scored, min_score=60):
    shown = [s for s in scored if s["score"] >= min_score]
    if not shown:
        print(f"No candidates scored >= {min_score}.")
        return
    for s in shown:
        print(f"\n{s['name']} ({s['team']} vs {s['opp_team']}) - {s['score']} {s['tier']}")
        for r in s["reasons"]:
            print(f"  - {r}")


# ---------------------------------------------------------------------------
# Self-calibration
# ---------------------------------------------------------------------------

SCORE_BUCKETS = [(0, 60), (60, 70), (70, 80), (80, 90), (90, 101)]


def _bucket_label(lo, hi):
    return f"{lo}-{hi - 1}" if hi <= 100 else f"{lo}-100"


def grade_dnp_scores(date_str):
    """Once date_str's games are Final, compare the snapshot saved by
    score_today against the real outcome (via get_game_start_data - the
    same source start_history.py backfills from) and roll into
    data/results/dnp_score_results.json's calibration table. Recomputed
    from scratch from all dates on every run, same "no incremental drift"
    discipline as the rest of this codebase's results files."""
    snapshot_path = os.path.join(SCORES_DIR, f"{date_str}.json")
    if not os.path.exists(snapshot_path):
        print(f"dnp_score: no score snapshot for {date_str} - nothing to grade.")
        return

    with open(snapshot_path, encoding="utf-8") as f:
        snapshot = json.load(f)

    game_pk_cache = {}
    graded = []
    for c in snapshot["candidates"]:
        game_pk = c.get("game_pk")
        pid = c.get("player_id")
        if game_pk is None or pid is None:
            continue
        if game_pk not in game_pk_cache:
            game_pk_cache[game_pk] = get_game_start_data(game_pk)
        game_data = game_pk_cache[game_pk]
        if game_data is None:
            continue
        actual_started = None
        for side in ("home", "away"):
            for p in game_data[side]["players"]:
                if p.get("player_id") == pid:
                    actual_started = p["started"]
                    break
            if actual_started is not None:
                break
        if actual_started is None:
            continue  # pitcher-filtered or not found - can't grade
        graded.append({
            "player_id": pid, "name": c["name"], "predicted_score": c["score"],
            "actual_started": actual_started,
        })

    data = {"dates": {}, "calibration": {}}
    if os.path.exists(RESULTS_PATH):
        try:
            with open(RESULTS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    data.setdefault("dates", {})
    data["dates"][date_str] = graded

    all_graded = [g for dd in data["dates"].values() for g in dd]
    calibration = {}
    for lo, hi in SCORE_BUCKETS:
        bucket = [g for g in all_graded if lo <= g["predicted_score"] < hi]
        n = len(bucket)
        dnp_count = sum(1 for g in bucket if not g["actual_started"])
        calibration[_bucket_label(lo, hi)] = {
            "n": n, "actual_dnp_rate": round(100 * dnp_count / n, 1) if n else None,
        }
    data["calibration"] = calibration

    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    dnp_today = sum(1 for g in graded if not g["actual_started"])
    print(f"dnp_score: graded {len(graded)} candidates for {date_str} ({dnp_today} actually DNP'd)")
    print("Calibration (predicted bucket -> actual DNP rate):")
    for label, c in calibration.items():
        rate = f"{c['actual_dnp_rate']}%" if c["actual_dnp_rate"] is not None else "n/a"
        print(f"  {label}: {rate} (n={c['n']})")


def main():
    parser = argparse.ArgumentParser(description="MLB DNP probability score")
    parser.add_argument("--today", action="store_true", help="score the most recent data/{date}.json")
    parser.add_argument("--date", default=None, help="score a specific date YYYY-MM-DD")
    parser.add_argument("--grade", default=None, help="grade a previously-scored date YYYY-MM-DD")
    parser.add_argument("--min-score", type=int, default=60, help="only print candidates >= this score")
    args = parser.parse_args()

    if args.grade:
        grade_dnp_scores(args.grade)
        return

    if args.today:
        import glob
        files = sorted(glob.glob(os.path.join("data", "20*-*-*.json")))
        if not files:
            print("No data/{date}.json files found.")
            return
        date_str = os.path.splitext(os.path.basename(files[-1]))[0]
    elif args.date:
        date_str = args.date
    else:
        parser.error("specify --today, --date, or --grade")
        return

    scored = score_today(date_str)
    print(f"Scored {len(scored)} players for {date_str}.\n")
    print_ranked(scored, min_score=args.min_score)


if __name__ == "__main__":
    main()
