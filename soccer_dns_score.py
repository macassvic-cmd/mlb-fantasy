"""
Soccer DNS scoring - three separate, orthogonal scores, same architecture
and same discipline as dnp_score.py's MLB rework (2026-09-13 Phase 2.1):

  dns_score        - P(will NOT start), pure evidence. NEVER influenced by
                      kickoff time or lineup-release ETA - those are
                      urgency signals, not evidence.
  confidence_score  - how much evidence backs dns_score, not how certain
                      the prediction is. Caps below 100 whenever the
                      official lineup hasn't posted.
  urgency_score     - how time-critical acting on this candidate is right
                      now (kickoff proximity, lineup-release-ETA
                      proximity). All timing signals live here only.

Status priors below are transparent, hand-set heuristic features (item 8
of the soccer DNS request) - none of this is backtested/calibrated yet.
Disagreement between sources is preserved, never erased: a predicted-start
consensus contribution and a predicted-bench contribution can both appear
in the same contributions list at once (see soccer_adapter.py's
_build_predicted_xi_sources / item 4).
"""

from datetime import datetime, timezone

from stale_lines import _parse_iso

BASE_SCORE = 3
CONFIRMED_NOT_STARTING_SCORE = 97  # official squad/lineup data confirms absence or non-starter status
CONFIRMED_STARTING_SCORE = 3

MIN_MATCHES_FOR_RATE_SIGNAL = 3
HISTORY_WEIGHT_FRACTION = 0.5  # softer than MLB's 1.0 - squads rotate more across competitions/cups

# --- Transfermarkt (confirmed injury/suspension list) ---------------------
TRANSFERMARKT_INJURY_WEIGHT = 45
TRANSFERMARKT_RETURN_BEFORE_FIXTURE_DISCOUNT = 20  # expected back before this specific fixture

# --- RotoWire (RSS-derived status tier) - see watchlist_classifiers.py ----
ROTOWIRE_STATUS_PRIOR = {
    "hard_out": 50, "fifty-fifty": 30, "late-call": 28, "fitness-test": 26,
    "monitored": 15, "assessed": 12, "option": 18, "news_mention": 8, "reversal": -20,
}

# --- X (trust-tier-scaled) -------------------------------------------------
X_INJURY_PRIOR = {"out": 50, "doubtful": 30, "questionable": 22, "late_fitness_test": 28, "missed_training": 20}
X_START_SIGNAL_PRIOR = {"confirmed_start": -25, "returning": -15, "bench_risk": 30}
X_TRUST_TIER_SCALE = {1: 1.0, 2: 0.9, 3: 0.6, 4: 0.35, 5: 0.15}

# --- Usage-pattern signals --------------------------------------------------
FIRST_RECENT_START_DISCOUNT = -10
FREQUENT_SUB_BONUS = 15
ROTATION_PLAYER_BONUS = 8

# --- Predicted-XI consensus - both directions kept visible ------------------
CONSENSUS_START_DISCOUNT_PER_SOURCE = -8
CONSENSUS_BENCH_BONUS_PER_SOURCE = 10

# --- Confidence weights ------------------------------------------------------
CONFIDENCE_HISTORY = 25
CONFIDENCE_OFFICIAL_STATUS = 30  # 0 whenever official_status == "not_yet_posted"
CONFIDENCE_ANY_ENRICHMENT = 20
CONFIDENCE_PREDICTED_XI = 15
CONFIDENCE_FRESHNESS = 10  # always fresh (live scoring), included for symmetry with dnp_score.py

# --- Urgency (soccer-specific ETA) ------------------------------------------
ASSUMED_LINEUP_LEAD_MINUTES = 60  # soccer official lineups typically release ~60min pre-kickoff (unvalidated placeholder, same honesty as MLB's ASSUMED_LINEUP_LEAD_MINUTES)
URGENCY_OVERDUE_RELEASE = 45
URGENCY_RELEASE_IMMINENT = 35
URGENCY_RELEASE_APPROACHING = 15
URGENCY_KICKOFF_SOON = 25
URGENCY_KICKOFF_NEAR = 10
URGENCY_RESOLVED = 5


def _score_dns(*, transfermarkt_hit, rotowire_hit, x_hit, starts_last_5, starts_last_10,
                first_recent_start, frequent_substitute, rotation_player,
                predicted_start_sources, predicted_bench_sources, event_date):
    contributions = []
    score = BASE_SCORE

    wins5, losses5, n5 = starts_last_5
    if n5 >= MIN_MATCHES_FOR_RATE_SIGNAL:
        rate = wins5 / n5
        contrib = round((1 - rate) * 100 * HISTORY_WEIGHT_FRACTION)
        contributions.append((f"{round(100 * (1 - rate))}% non-start rate last {n5} matches", contrib))
        score += contrib

    if transfermarkt_hit:
        weight = TRANSFERMARKT_INJURY_WEIGHT
        label = f"Transfermarkt: {transfermarkt_hit['section']} - {transfermarkt_hit['reason']}"
        ret_date = transfermarkt_hit.get("expected_return_date")
        if ret_date and event_date:
            try:
                fixture_dt = _parse_iso(event_date)
                if fixture_dt and ret_date < fixture_dt.date():
                    weight -= TRANSFERMARKT_RETURN_BEFORE_FIXTURE_DISCOUNT
                    label += " (expected back before this fixture)"
            except Exception:
                pass
        contributions.append((label, weight))
        score += weight

    if rotowire_hit:
        key = rotowire_hit.get("rotowire_status_raw") or rotowire_hit.get("rotowire_status_normalized")
        weight = ROTOWIRE_STATUS_PRIOR.get(key, 0)
        if weight:
            contributions.append((f"RotoWire: {key}", weight))
            score += weight

    if x_hit:
        scale = X_TRUST_TIER_SCALE.get(x_hit.get("author_trust_tier"), 0.15)
        author = x_hit.get("author_username") or "unverified"
        tier = x_hit.get("author_trust_tier")
        inj = x_hit.get("extracted_injury")
        if inj and inj in X_INJURY_PRIOR:
            w = round(X_INJURY_PRIOR[inj] * scale)
            contributions.append((f"X ({author}, tier {tier}): {inj}", w))
            score += w
        start_sig = x_hit.get("extracted_start_signal")
        if start_sig and start_sig in X_START_SIGNAL_PRIOR:
            w = round(X_START_SIGNAL_PRIOR[start_sig] * scale)
            contributions.append((f"X ({author}, tier {tier}): {start_sig}", w))
            score += w

    if first_recent_start:
        contributions.append(("First start of recent stretch - rotation risk still real but trending up",
                               FIRST_RECENT_START_DISCOUNT))
        score += FIRST_RECENT_START_DISCOUNT
    if frequent_substitute:
        contributions.append(("Frequent-substitute usage pattern", FREQUENT_SUB_BONUS))
        score += FREQUENT_SUB_BONUS
    elif rotation_player:
        contributions.append(("Rotation-risk usage pattern (inconsistent starts)", ROTATION_PLAYER_BONUS))
        score += ROTATION_PLAYER_BONUS

    # Consensus - BOTH directions kept, never erased even when they conflict.
    if predicted_start_sources:
        total = len(predicted_start_sources) + len(predicted_bench_sources)
        w = CONSENSUS_START_DISCOUNT_PER_SOURCE * len(predicted_start_sources)
        contributions.append((f"Predicted starter {len(predicted_start_sources)}/{total} sources "
                               f"({', '.join(predicted_start_sources)})", w))
        score += w
    if predicted_bench_sources:
        total = len(predicted_start_sources) + len(predicted_bench_sources)
        w = CONSENSUS_BENCH_BONUS_PER_SOURCE * len(predicted_bench_sources)
        contributions.append((f"Predicted bench {len(predicted_bench_sources)}/{total} sources "
                               f"({', '.join(predicted_bench_sources)})", w))
        score += w

    if not contributions:
        contributions.append(("No enrichment signals available", 0))

    return max(0, min(100, round(score))), contributions


def _score_confidence(*, official_status, starts_last_5, transfermarkt_hit, rotowire_hit, x_hit,
                       predicted_start_sources, predicted_bench_sources):
    confidence = 0
    if starts_last_5[2] >= MIN_MATCHES_FOR_RATE_SIGNAL:
        confidence += CONFIDENCE_HISTORY
    if official_status in ("confirmed_starting", "confirmed_not_starting"):
        confidence += CONFIDENCE_OFFICIAL_STATUS
    if transfermarkt_hit or rotowire_hit or x_hit:
        confidence += CONFIDENCE_ANY_ENRICHMENT
    if predicted_start_sources or predicted_bench_sources:
        confidence += CONFIDENCE_PREDICTED_XI
    confidence += CONFIDENCE_FRESHNESS
    return max(0, min(100, confidence))


def _score_urgency(*, official_status, event_date, now=None):
    if official_status != "not_yet_posted" and official_status != "unknown":
        return URGENCY_RESOLVED, ["Lineup already resolved - no more waiting window"]

    now = now or datetime.now(timezone.utc)
    kickoff = _parse_iso(event_date) if event_date else None
    if kickoff is None:
        return 0, ["Kickoff time unknown - cannot estimate urgency"]

    minutes_to_kickoff = (kickoff - now).total_seconds() / 60
    minutes_to_release = minutes_to_kickoff - ASSUMED_LINEUP_LEAD_MINUTES

    score = 0
    reasons = []
    if minutes_to_release <= 0:
        score += URGENCY_OVERDUE_RELEASE
        reasons.append(f"Past the assumed lineup-release window ({round(-minutes_to_release)} min overdue) and still unposted")
    elif minutes_to_release <= 20:
        score += URGENCY_RELEASE_IMMINENT
        reasons.append(f"Lineup release expected in ~{round(minutes_to_release)} min")
    elif minutes_to_release <= 60:
        score += URGENCY_RELEASE_APPROACHING
        reasons.append(f"Lineup release expected in ~{round(minutes_to_release)} min")

    if minutes_to_kickoff <= 30:
        score += URGENCY_KICKOFF_SOON
        reasons.append("Kickoff under 30 minutes away")
    elif minutes_to_kickoff <= 90:
        score += URGENCY_KICKOFF_NEAR
        reasons.append("Kickoff under 90 minutes away")

    reasons.append("Cross-book prop-removal timing not tracked for soccer yet")
    return max(0, min(100, round(score))), reasons


def score_candidate(*, official_status, transfermarkt_hit, rotowire_hit, x_hit, starts_last_5, starts_last_10,
                     first_recent_start, frequent_substitute, rotation_player,
                     predicted_start_sources, predicted_bench_sources, event_date):
    """(dns_score, confidence_score, urgency_score, evidence_count,
    contributions, urgency_reasons)."""
    if official_status == "confirmed_not_starting":
        contributions = [("Official squad/lineup data confirms NOT starting", CONFIRMED_NOT_STARTING_SCORE)]
        return (CONFIRMED_NOT_STARTING_SCORE, 90, URGENCY_RESOLVED, 1, contributions,
                ["Lineup already resolved - no more waiting window"])
    if official_status == "confirmed_starting":
        contributions = [("Official lineup confirms starting", CONFIRMED_STARTING_SCORE)]
        return (CONFIRMED_STARTING_SCORE, 90, URGENCY_RESOLVED, 1, contributions,
                ["Lineup already resolved - no more waiting window"])

    dns, contributions = _score_dns(
        transfermarkt_hit=transfermarkt_hit, rotowire_hit=rotowire_hit, x_hit=x_hit,
        starts_last_5=starts_last_5, starts_last_10=starts_last_10,
        first_recent_start=first_recent_start, frequent_substitute=frequent_substitute,
        rotation_player=rotation_player, predicted_start_sources=predicted_start_sources,
        predicted_bench_sources=predicted_bench_sources, event_date=event_date,
    )
    confidence = _score_confidence(
        official_status=official_status, starts_last_5=starts_last_5,
        transfermarkt_hit=transfermarkt_hit, rotowire_hit=rotowire_hit, x_hit=x_hit,
        predicted_start_sources=predicted_start_sources, predicted_bench_sources=predicted_bench_sources,
    )
    urgency, urgency_reasons = _score_urgency(official_status=official_status, event_date=event_date)

    evidence_count = sum([
        starts_last_5[2] >= MIN_MATCHES_FOR_RATE_SIGNAL,
        transfermarkt_hit is not None,
        rotowire_hit is not None,
        x_hit is not None,
        bool(predicted_start_sources or predicted_bench_sources),
    ])

    return dns, confidence, urgency, evidence_count, contributions, urgency_reasons


def combined_priority(dns_score, urgency_score):
    """Sort key for "hunting priority" - same 0.7/0.3 dns/urgency blend as
    dnp_score.combined_priority (MLB), kept as its own small function
    here (not imported) so this module stays independent of dnp_score.py,
    but deliberately the same formula/weights - a documented choice, not
    a derived one, same as the MLB side."""
    return round(dns_score * 0.7 + urgency_score * 0.3, 1)


def tier_label(dns_score):
    if dns_score >= 90:
        return "DNS SNIPER"
    if dns_score >= 80:
        return "Very High"
    if dns_score >= 70:
        return "Strong Watch"
    if dns_score >= 60:
        return "Watch"
    return "Ignore"
