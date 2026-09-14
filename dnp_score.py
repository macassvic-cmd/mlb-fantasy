"""
DNP Score v2 (2026-09-13, Phase 2.1) - three separate, orthogonal scores
for an MLB position player with a live prop:

  dnp_score        - P(will NOT start), a pure evidence-based estimate.
                      NEVER influenced by time-to-game, lineup-release ETA,
                      or "early game" - those are timing/action signals,
                      not evidence the player will be absent. See
                      score_urgency for all of that.
  confidence_score  - how much evidence backs the dnp_score, NOT how
                      certain the prediction is. A player with no lineup
                      source posted yet can score high or low on dnp_score,
                      but can never hit 100 confidence, because "no lineup
                      posted" is itself a missing evidence source.
  urgency_score     - how time-critical acting on this candidate is right
                      now (lineup-release proximity, first-pitch proximity,
                      whether the projected status just changed). This is
                      where every timing signal lives.

Phase 1 (score_player/score_today, the pipeline path) mixed "not yet
confirmed" (a pure evidence gap) with "early game" (a timing signal) into
one additive score - that conflation is exactly what this rework removes.
A years-long everyday starter who simply hasn't had today's lineup posted
yet should score LOW on dnp_score (their prior says they almost always
start) and MODERATE-TO-HIGH on urgency (if game time is close), not
"medium-high" on one blended number the way v1 did.

Every weight below is still a transparent, hand-set heuristic - none of
this is backtested. See grade_dnp_scores for the self-calibration loop.

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
from scrapers.betr import normalize_name
from scrapers.mlb_api import get_game_start_data
from stale_lines import _parse_iso

SCORES_DIR = os.path.join("data", "dnp_scores")
RESULTS_PATH = os.path.join("data", "results", "dnp_score_results.json")
WATCHLIST_EVENTS_PATH = os.path.join("data", "watchlist", "events.jsonl")

# Soft-signal tiers (rss_news_classifier.SOFT_TIER_PATTERNS) that indicate
# elevated bench/sit risk - "probable" and "nearing-return" mean the
# OPPOSITE (a player working back toward playing), so they're deliberately
# excluded here.
RISK_NEWS_TIERS = {"doubtful", "questionable", "game-time-decision", "day-to-day", "news-mention"}
NEWS_LOOKBACK_HOURS = 48
MIN_GAMES_FOR_RATE_SIGNAL = 3  # below this, a rate is noise - skip the signal entirely

# ---------------------------------------------------------------------------
# DNP score weights - PURE evidence about start likelihood. No game_start,
# no "is_early_game", no ETA of any kind belongs in this section - see
# score_urgency for all timing signals.
# ---------------------------------------------------------------------------
CONFIRMED_NOT_STARTING_SCORE = 97  # lineup posted, genuinely absent - near-certainty, not a heuristic blend
CONFIRMED_STARTING_SCORE = 3       # lineup posted, this player IS in it - small residual late-scratch risk
DEFAULT_PRIOR_NO_HISTORY = 15      # no usable start history at all - league-average bench-rate placeholder,
                                    # not a real backtested rate yet (see module docstring)
PLATOON_WEIGHT = 20                # scaled by how much lower their vs-hand rate is than their overall recent rate
STREAK_FATIGUE_MAX = 10            # consecutive-starts fatigue, capped
STREAK_FATIGUE_MIN_GAMES = 6       # streak length before fatigue starts counting
CATCHER_REST_BONUS = 8             # extra bump for a catcher on a 4+ game streak
CATCHER_REST_MIN_STREAK = 4
NEWS_RISK_BONUS = 12

# ---------------------------------------------------------------------------
# Confidence weights - evidence QUALITY/COMPLETENESS, not certainty. Sums
# to 100 only when every source is present AND a lineup has posted -
# "no lineup source posted" alone caps this at 70 (30 history + 15 hand +
# 15 freshness + 10 news, if every one of THOSE also happens to be present -
# realistically much lower), which is the explicit requirement: a
# not-yet-posted candidate can never show 100 confidence.
# ---------------------------------------------------------------------------
CONFIDENCE_HISTORY_MAX = 30       # scaled by min(n, 10)/10 - partial credit for a partial sample
CONFIDENCE_OPP_HAND_KNOWN = 15
CONFIDENCE_LINEUP_POSTED = 30     # 0 whenever start_status == "not_yet_posted"/"unknown"
CONFIDENCE_NEWS_AVAILABLE = 10
CONFIDENCE_FRESHNESS_MAX = 15

# ---------------------------------------------------------------------------
# Urgency weights - ALL timing/action signals live here, never in dnp_score.
# ASSUMED_LINEUP_LEAD_MINUTES is a placeholder (MLB lineups typically post
# a few hours pre-game) pending the real per-team learned release-time
# model the original brainstorm described - not built yet, documented as a
# known gap rather than silently pretended-precise.
# ---------------------------------------------------------------------------
ASSUMED_LINEUP_LEAD_MINUTES = 180
URGENCY_OVERDUE_RELEASE = 45       # past the assumed release window, still not posted
URGENCY_RELEASE_IMMINENT = 35      # within 45 min of the assumed release window
URGENCY_RELEASE_APPROACHING = 15   # within 2 hours of the assumed release window
URGENCY_FIRST_PITCH_SOON = 25      # under 30 minutes to first pitch
URGENCY_FIRST_PITCH_NEAR = 10      # under 90 minutes to first pitch
URGENCY_STATUS_CHANGED = 15        # projected status flipped since the last check this same day
URGENCY_RESOLVED = 5               # lineup already posted either way - no more "sniping" window, informational only


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


def _opp_pitcher_hand_from_row(row):
    """Derive today's opposing starter's handedness from bat_side +
    platoon.platoon_same_hand (both already computed by the pipeline) -
    avoids a second lookup for something we already have implicitly.
    Only used by the pipeline path (score_player) - the live Dabble path
    resolves this directly via dabble_adapter._resolve_player_context."""
    bat_side = row.get("bat_side")
    same_hand = (row.get("platoon") or {}).get("platoon_same_hand")
    if bat_side not in ("L", "R") or same_hand is None:
        return None
    return bat_side if same_hand else ("R" if bat_side == "L" else "L")


# ---------------------------------------------------------------------------
# DNP score - pure evidence, no timing signals
# ---------------------------------------------------------------------------

def score_dnp(*, player_id, name, position, start_status, opp_pitcher_hand, history, news_risk):
    """(dnp_score_0_100, contributions: [(label, points), ...]). Every
    point on the score is itemized here in the same units it's added in,
    so a caller can print an exact diagnostic breakdown - see
    format_diagnostic(). Contributions with 0 points (e.g. "no lineup
    source posted yet") are kept in the list as informational notes."""
    if start_status == "confirmed_not_starting":
        return CONFIRMED_NOT_STARTING_SCORE, [("Lineup posted - CONFIRMED not in the starting lineup",
                                                CONFIRMED_NOT_STARTING_SCORE)]
    if start_status == "confirmed_starting":
        return CONFIRMED_STARTING_SCORE, [("Confirmed starting in today's lineup", CONFIRMED_STARTING_SCORE)]

    # "not_yet_posted" / "unknown": build a genuine prior estimate. No flat
    # "unconfirmed" penalty here - an everyday starter whose lineup simply
    # hasn't posted yet should score LOW, not medium-high, purely because
    # of this line of reasoning (see module docstring for why v1 got this
    # wrong via NOT_CONFIRMED_WEIGHT + GETAWAY_DAY_BONUS).
    contributions = []
    wins, losses, n = start_history.start_rate(history, player_id, n_games=10)
    if n >= MIN_GAMES_FOR_RATE_SIGNAL:
        recent_rate = wins / n
        base = round((1 - recent_rate) * 100)
        contributions.append((f"{round(100 * (1 - recent_rate))}% historical non-start rate (n={n})", base))
    else:
        recent_rate = None
        contributions.append((f"No usable start history (n={n}) - league-average prior", DEFAULT_PRIOR_NO_HISTORY))

    if opp_pitcher_hand and n >= MIN_GAMES_FOR_RATE_SIGNAL:
        hw, hl, hn = start_history.start_rate_vs_hand(history, player_id, opp_pitcher_hand, n_games=15)
        if hn >= MIN_GAMES_FOR_RATE_SIGNAL:
            hand_rate = hw / hn
            diff = (recent_rate or 1.0) - hand_rate
            if diff >= 0.15:
                contributions.append((
                    f"Platoon: starts only {round(100 * hand_rate)}% vs {opp_pitcher_hand}HP "
                    f"(n={hn}, overall {round(100 * (recent_rate or 0))}%)",
                    round(diff * PLATOON_WEIGHT),
                ))

    streak = start_history.consecutive_starts(history, player_id)
    if streak >= STREAK_FATIGUE_MIN_GAMES:
        fatigue = min((streak - (STREAK_FATIGUE_MIN_GAMES - 1)) * 2, STREAK_FATIGUE_MAX)
        contributions.append((f"On a {streak}-game start streak", fatigue))
        if position == "C" and streak >= CATCHER_REST_MIN_STREAK:
            contributions.append((f"Catcher on a {streak}-game streak - rest risk elevated", CATCHER_REST_BONUS))
    elif position == "C" and streak >= CATCHER_REST_MIN_STREAK:
        contributions.append((f"Catcher on a {streak}-game streak - rest risk elevated", CATCHER_REST_BONUS))

    news = news_risk.get(normalize_name(name or ""))
    if news:
        tier, headline = news
        snippet = (headline or "")[:80]
        label = f"Recent news ({tier}): {snippet}" if snippet else f"Recent news tier: {tier}"
        contributions.append((label, NEWS_RISK_BONUS))

    contributions.append(("No projected lineup source posted yet", 0))

    score = max(0, min(100, round(sum(c[1] for c in contributions))))
    return score, contributions


# ---------------------------------------------------------------------------
# Confidence score - evidence completeness, not prediction certainty
# ---------------------------------------------------------------------------

def _freshness_credit(scraped_at):
    if not scraped_at:
        return 0
    try:
        dt = _parse_iso(scraped_at) if isinstance(scraped_at, str) else None
    except (ValueError, TypeError):
        dt = None
    if dt is None:
        return 0
    age_minutes = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    if age_minutes <= 30:
        return CONFIDENCE_FRESHNESS_MAX
    if age_minutes <= 90:
        return round(CONFIDENCE_FRESHNESS_MAX * 0.5)
    return 0


def score_confidence(*, start_status, recent_n, opp_hand_known, news_available, scraped_at=None):
    """(confidence_0_100, evidence_count). A not-yet-posted candidate loses
    the full CONFIDENCE_LINEUP_POSTED weight unconditionally - that's the
    single largest bucket, so this can never reach 100 without a real
    posted lineup, regardless of how complete every other signal is."""
    evidence_count = 0
    confidence = 0

    history_credit = round(min(recent_n, 10) / 10 * CONFIDENCE_HISTORY_MAX)
    confidence += history_credit
    if recent_n > 0:
        evidence_count += 1

    if opp_hand_known:
        confidence += CONFIDENCE_OPP_HAND_KNOWN
        evidence_count += 1

    if start_status in ("confirmed_starting", "confirmed_not_starting"):
        confidence += CONFIDENCE_LINEUP_POSTED
        evidence_count += 1

    if news_available:
        confidence += CONFIDENCE_NEWS_AVAILABLE
        evidence_count += 1

    freshness_credit = _freshness_credit(scraped_at)
    confidence += freshness_credit
    if freshness_credit > 0:
        evidence_count += 1

    return max(0, min(100, round(confidence))), evidence_count


# ---------------------------------------------------------------------------
# Urgency score - ALL timing signals live here
# ---------------------------------------------------------------------------

def score_urgency(*, start_status, game_start, status_changed_recently=False, now=None):
    """(urgency_0_100, reasons: list[str]). Deliberately the ONLY place
    game_start/time-to-first-pitch/lineup-release-ETA are used anywhere in
    this module - dnp_score must never see them (see module docstring)."""
    if start_status != "not_yet_posted" and start_status != "unknown":
        return URGENCY_RESOLVED, ["Lineup already resolved - no more waiting window"]

    now = now or datetime.now(timezone.utc)
    game_dt = _parse_iso(game_start) if game_start else None
    if game_dt is None:
        return 0, ["Game time unknown - cannot estimate urgency"]

    minutes_to_first_pitch = (game_dt - now).total_seconds() / 60
    minutes_to_expected_release = minutes_to_first_pitch - ASSUMED_LINEUP_LEAD_MINUTES

    score = 0
    reasons = []
    if minutes_to_expected_release <= 0:
        score += URGENCY_OVERDUE_RELEASE
        reasons.append(f"Past the assumed lineup-release window ({round(-minutes_to_expected_release)} min "
                        f"overdue) and still unposted")
    elif minutes_to_expected_release <= 45:
        score += URGENCY_RELEASE_IMMINENT
        reasons.append(f"Lineup release expected in ~{round(minutes_to_expected_release)} min")
    elif minutes_to_expected_release <= 120:
        score += URGENCY_RELEASE_APPROACHING
        reasons.append(f"Lineup release expected in ~{round(minutes_to_expected_release)} min")

    if minutes_to_first_pitch <= 30:
        score += URGENCY_FIRST_PITCH_SOON
        reasons.append("First pitch under 30 minutes away")
    elif minutes_to_first_pitch <= 90:
        score += URGENCY_FIRST_PITCH_NEAR
        reasons.append("First pitch under 90 minutes away")

    if status_changed_recently:
        score += URGENCY_STATUS_CHANGED
        reasons.append("Projected status changed since the last check")

    reasons.append("Cross-book prop-removal timing not available yet")  # honest gap - see dabble_adapter module docstring

    return max(0, min(100, round(score))), reasons


def tier_label(dnp_score):
    if dnp_score >= 90:
        return "DNP SNIPER"
    if dnp_score >= 80:
        return "Very High"
    if dnp_score >= 70:
        return "Strong Watch"
    if dnp_score >= 60:
        return "Watch"
    return "Ignore"


def combined_priority(dnp_score, urgency_score):
    """Sort key for "hunting priority" - DNP stays primary (0.7 weight),
    urgency breaks ties / boosts genuinely time-critical candidates (0.3).
    A documented choice, not a derived one - there's no "correct" blend."""
    return round(dnp_score * 0.7 + urgency_score * 0.3, 1)


def format_diagnostic(name, dnp_score, confidence_score, urgency_score, contributions):
    lines = [f"{name} - DNP {dnp_score} | Conf {confidence_score} | Urgency {urgency_score}"]
    for label, points in contributions:
        if points > 0:
            lines.append(f"  +{points} {label}")
        elif points == 0:
            lines.append(f"  {label}")
        else:
            lines.append(f"  {points} {label}")
    return "\n".join(lines)


def score_player(row, history, news_risk):
    """Pipeline-path candidate (a dict from data/{date}.json, NOT
    report.build_row's flattened version). data/{date}.json only ever
    contains players guessed INTO a lineup slot, so "confirmed_not_starting"
    never applies here - only "confirmed_starting" or "not_yet_posted".
    Returns a dict with the full v2 output schema (dnp_score/
    confidence_score/urgency_score/...)."""
    start_status = "confirmed_starting" if row.get("lineup_confirmed", True) else "not_yet_posted"
    pid = row.get("player_id")
    opp_hand = _opp_pitcher_hand_from_row(row)

    dnp, contributions = score_dnp(
        player_id=pid, name=row.get("name"), position=row.get("position"),
        start_status=start_status, opp_pitcher_hand=opp_hand, history=history, news_risk=news_risk,
    )
    wins, losses, n = start_history.start_rate(history, pid, n_games=10)
    news_hit = news_risk.get(normalize_name(row.get("name", "")))
    confidence, evidence_count = score_confidence(
        start_status=start_status, recent_n=n, opp_hand_known=opp_hand is not None,
        news_available=news_hit is not None,
    )
    urgency, urgency_reasons = score_urgency(start_status=start_status, game_start=row.get("game_date_utc"))

    return {
        "dnp_score": dnp, "confidence_score": confidence, "urgency_score": urgency,
        "evidence_count": evidence_count, "combined_priority": combined_priority(dnp, urgency),
        "projected_start_status": start_status,
        "top_reasons": [label for label, _ in contributions],
        "urgency_reasons": urgency_reasons,
        "contributions": contributions,
    }


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
        result = score_player(row, history, news_risk)
        scored.append({
            "player_id": row.get("player_id"),
            "name": row.get("name"),
            "team": row.get("team_name"),
            "opp_team": row.get("opp_team_name"),
            "game_pk": row.get("game_pk"),
            "position": row.get("position"),
            "batting_order": row.get("batting_order"),
            "lineup_status": row.get("lineup_status"),
            "tier": tier_label(result["dnp_score"]),
            **result,
        })
    scored.sort(key=lambda r: r["dnp_score"], reverse=True)

    os.makedirs(SCORES_DIR, exist_ok=True)
    with open(os.path.join(SCORES_DIR, f"{date_str}.json"), "w", encoding="utf-8") as f:
        json.dump({"date": date_str, "candidates": scored}, f, indent=2)

    return scored


def print_ranked(scored, min_score=60):
    shown = [s for s in scored if s["dnp_score"] >= min_score]
    if not shown:
        print(f"No candidates scored >= {min_score}.")
        return
    for s in shown:
        print()
        print(format_diagnostic(f"{s['name']} ({s['team']} vs {s['opp_team']})",
                                 s["dnp_score"], s["confidence_score"], s["urgency_score"], s["contributions"]))


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
    discipline as the rest of this codebase's results files.

    c.get("dnp_score", c.get("score")) tolerates snapshots saved by the
    pre-2026-09-13 v1 scorer (field was named "score") as well as v2's
    "dnp_score" - old snapshots already committed shouldn't become
    ungradeable just because the field was renamed."""
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
        predicted_score = c.get("dnp_score", c.get("score"))
        if game_pk is None or pid is None or predicted_score is None:
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
            "player_id": pid, "name": c["name"], "predicted_score": predicted_score,
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
