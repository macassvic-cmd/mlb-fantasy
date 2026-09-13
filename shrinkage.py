"""Bayesian (Beta-Binomial) shrinkage for per-player rate stats.

Every per-player rate in this codebase (Top-25 record, fire/hot tiers, UNDER
band record by player) has historically been the raw observed rate with a
hard sample-size floor gating whether it's shown at all (TOP25_RECORD_MIN_N_FOR_RATE,
FIRE_MIN_N/HOT_MIN_N) - a player either has "enough" appearances to trust
their raw rate, or they don't get a rate at all. That's a step function, not
regularization: a player at 4-2 (66.7%) gets treated identically to one at
40-20 (66.7%) once both clear the same n floor.

shrink_rate() replaces the step function with a continuous pull toward a
pooled baseline, weighted by sample size via a Beta-Binomial posterior mean:
a small-n player (e.g. 4-2) lands close to the baseline, a large-n player
(e.g. 23-8) keeps most of their own signal. Added 2026-09-13 as part of the
end-of-season sharpening pass - see report.py/tracker.py callers for how the
baseline is computed for each specific rate (never a hardcoded constant;
always that cohort's own pooled wins/losses so it self-updates).
"""

# Pseudo-observations of the baseline rate the shrinkage prior is worth.
# n=20 means: a player needs roughly 20 of their own decisions before their
# raw rate outweighs the baseline in the blend. Picked to sit a bit above
# TOP25_RECORD_MIN_N_FOR_RATE (8) and FIRE_MIN_N (15) - the point where those
# hard cutoffs used to declare "enough data" - so shrinkage doesn't fully let
# go of the baseline right at the old floor. Revisit during the offseason
# deep-analysis once there's a full season of shrunk-vs-raw comparison to
# tune against.
SHRINKAGE_PRIOR_STRENGTH = 20


def shrink_rate(wins, losses, baseline_rate, prior_strength=SHRINKAGE_PRIOR_STRENGTH):
    """Beta-Binomial posterior mean: blend of the player's own rate and the
    baseline, weighted by how many decisions the player has vs. the prior's
    pseudo-count. n=0 returns the baseline outright (no data to shrink)."""
    n = wins + losses
    if n <= 0:
        return baseline_rate
    return (wins + prior_strength * baseline_rate) / (n + prior_strength)


def pooled_rate(wins, losses, default=0.5):
    """Simple pooled win rate, used to build the baseline a specific
    player's shrink_rate() call is pulled toward. Falls back to `default`
    (coin-flip) when there's no data at all yet, e.g. a brand-new cohort."""
    n = wins + losses
    if n <= 0:
        return default
    return wins / n
