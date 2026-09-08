"""
SGP correlation adjustment - blends a same-game-parlay group's
independent (naive product) fair probability toward that group's
single weakest leg, controlled by a correlation coefficient r in [0, 1]:

    P_correlated_group = P_independent_group + r * (P_min_leg - P_independent_group)

r=0 recovers the plain independent product exactly (no correlation
assumed). r=1 assumes the group is perfectly correlated - bounded by
its weakest leg (if the least-likely leg hits, the model assumes the
rest do too, since they're the same game). Multiple SGP groups (e.g.
two different games in one combined ticket) are then combined with each
other via the ordinary independent product - no cross-game correlation
is modeled, since they're genuinely different events.

VALIDATED (2026-09-08) against real CNM reference output: a 5-leg
parlay (3-leg SGP at r=0.33, 2-leg SGP at r=0.14) reported a correlated
fair value of 5.6% (CNM), vs. this formula's 5.6275% computed from our
own (already-validated) leg fair probabilities - matches within the
precision CNM's own 1-decimal-percent display allows. This is NOT from
CNM's own docs (the actual "correlation textbox" input format the user
referenced hasn't been shared here) - it's reverse-engineered from the
reference number, tried against several candidate correlation models by
hand before this one matched. Confident in the MATH; the INPUT SYNTAX
below (sgp_specs as (leg_count, r) tuples) is this project's own
placeholder interface, not CNM's actual textbox format - see
evaluate_correlated()'s docstring.
"""

from dataclasses import dataclass, field
from typing import List, Tuple

from combine import evaluate_leg
from devig import prob_to_american
from parser import parse_input


@dataclass
class SgpGroupResult:
    leg_count: int
    r: float
    leg_fair_probabilities: List[float] = field(default_factory=list)
    independent_probability: float = 0.0
    correlated_probability: float = 0.0


@dataclass
class CorrelatedEvaluation:
    groups: List[SgpGroupResult]
    uncorrelated_probability: float
    correlated_probability: float
    uncorrelated_fair_american_odds: float
    correlated_fair_american_odds: float
    method: str


def parse_sgp_specs(spec_str):
    """Parses this project's placeholder "correlation textbox" syntax:
    comma-separated leg_count:r pairs, e.g. "3:0.33,2:0.14" for a 3-leg
    SGP at r=0.33 followed by a 2-leg SGP at r=0.14 - NOT CNM's actual
    textbox format (unavailable here), see module docstring."""
    specs = []
    for part in spec_str.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            count_str, r_str = part.split(":")
            count, r = int(count_str), float(r_str)
        except ValueError:
            raise ValueError(f"Could not parse SGP group {part!r} - expected leg_count:r, e.g. '3:0.33'.")
        if count < 1:
            raise ValueError(f"SGP group leg_count must be >= 1, got {count} in {part!r}.")
        specs.append((count, r))
    if not specs:
        raise ValueError("No SGP groups parsed from empty input.")
    return specs


def correlated_group_probability(leg_fair_probs, r):
    if not (0.0 <= r <= 1.0):
        raise ValueError(f"r must be in [0, 1], got {r}.")
    if not leg_fair_probs:
        raise ValueError("leg_fair_probs must be non-empty.")
    independent = 1.0
    for p in leg_fair_probs:
        independent *= p
    p_min = min(leg_fair_probs)
    return independent + r * (p_min - independent)


def evaluate_correlated(odds_str, sgp_specs, method="multiplicative"):
    """sgp_specs: ordered list of (leg_count, r) tuples partitioning
    EVERY leg of a single comma-separated parlay (no "||" - correlation
    across independent OR-groups isn't a coherent concept, they're
    mutually exclusive tickets, not one combined bet) into same-game
    groups, in the same left-to-right order the legs appear in odds_str.
    A leg with no real correlation to anything else should be its own
    (1, 0.0) group - r=0 makes correlated_probability equal
    independent_probability for that "group," a no-op.

    NOT CNM's actual correlation-textbox input syntax (not available in
    this project) - a placeholder interface for this project's own
    /sgp Discord command until the real format is confirmed. The MATH
    (correlated_group_probability) is validated against real CNM output;
    this function's specific (leg_count, r) tuple shape is not.
    """
    parsed = parse_input(odds_str)
    if len(parsed.or_groups) != 1:
        raise ValueError("evaluate_correlated only supports a single parlay (no \"||\" groups).")
    legs = parsed.or_groups[0].legs

    total_legs = sum(count for count, _r in sgp_specs)
    if total_legs != len(legs):
        raise ValueError(f"sgp_specs covers {total_legs} legs but the input has {len(legs)} legs.")

    leg_evals = [evaluate_leg(leg, method) for leg in legs]

    idx = 0
    groups = []
    uncorrelated_overall = 1.0
    correlated_overall = 1.0
    for count, r in sgp_specs:
        group_probs = [le.fair_probability for le in leg_evals[idx:idx + count]]
        idx += count

        independent = 1.0
        for p in group_probs:
            independent *= p
        correlated = correlated_group_probability(group_probs, r)

        groups.append(SgpGroupResult(
            leg_count=count, r=r, leg_fair_probabilities=group_probs,
            independent_probability=independent, correlated_probability=correlated,
        ))
        uncorrelated_overall *= independent
        correlated_overall *= correlated

    uncorrelated_overall = min(max(uncorrelated_overall, 1e-9), 1 - 1e-9)
    correlated_overall = min(max(correlated_overall, 1e-9), 1 - 1e-9)

    return CorrelatedEvaluation(
        groups=groups,
        uncorrelated_probability=uncorrelated_overall,
        correlated_probability=correlated_overall,
        uncorrelated_fair_american_odds=prob_to_american(uncorrelated_overall),
        correlated_fair_american_odds=prob_to_american(correlated_overall),
        method=method,
    )
