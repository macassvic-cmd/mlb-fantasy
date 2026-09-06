"""
Ties parser.py's parsed structure to devig.py's math: devigs each leg's
own market, then combines legs into parlays (AND, comma) and groups
(OR, ||).

COMBINATION MATH:
  - Parlay (comma, within one OR-group): legs are treated as
    independent, so the combined fair probability is the product of
    each leg's own fair probability for "the side being wagered on"
    (index 0 of that leg's devigged market). Standard parlay math -
    no adjustment for correlation, since the input format gives no way
    to express same-game correlation directly (a true SGP correlation
    model would need its own covariance/joint data this tool doesn't
    have).
  - OR (||, between groups): each group (itself possibly a full parlay)
    is treated as an independent event; P(at least one group hits) =
    1 - product(1 - p_group_i) over all groups - the standard formula
    for the union of independent events, generalized beyond 2 groups.
"""

from dataclasses import dataclass, field
from typing import List

from devig import devig, prob_to_american
from parser import parse_input, ParsedLeg, ParlayGroup, ParsedInput


@dataclass
class LegEvaluation:
    leg: ParsedLeg
    devig_result: object  # devig.DevigResult for this leg's full market
    fair_probability: float  # devig_result.fair_probabilities[0] - "our side"
    fair_american_odds: float


@dataclass
class GroupEvaluation:
    group: ParlayGroup
    leg_evaluations: List[LegEvaluation]
    combined_fair_probability: float
    combined_fair_american_odds: float


@dataclass
class FullEvaluation:
    parsed: ParsedInput
    group_evaluations: List[GroupEvaluation]
    overall_fair_probability: float       # OR-combined across all groups (== single group's prob if only one)
    overall_fair_american_odds: float
    method: str


def evaluate_leg(leg: ParsedLeg, method: str) -> LegEvaluation:
    result = devig(leg.market.odds, method=method)
    fair_p = result.fair_probabilities[0]
    return LegEvaluation(leg=leg, devig_result=result, fair_probability=fair_p,
                          fair_american_odds=prob_to_american(fair_p))


def evaluate_group(group: ParlayGroup, method: str) -> GroupEvaluation:
    leg_evals = [evaluate_leg(leg, method) for leg in group.legs]
    combined_p = 1.0
    for le in leg_evals:
        combined_p *= le.fair_probability
    combined_p = min(max(combined_p, 1e-9), 1 - 1e-9)
    return GroupEvaluation(group=group, leg_evaluations=leg_evals,
                            combined_fair_probability=combined_p,
                            combined_fair_american_odds=prob_to_american(combined_p))


def evaluate(input_str: str, method: str = "multiplicative") -> FullEvaluation:
    parsed = parse_input(input_str)
    group_evals = [evaluate_group(g, method) for g in parsed.or_groups]

    if len(group_evals) == 1:
        overall_p = group_evals[0].combined_fair_probability
    else:
        miss_all = 1.0
        for ge in group_evals:
            miss_all *= (1.0 - ge.combined_fair_probability)
        overall_p = 1.0 - miss_all

    overall_p = min(max(overall_p, 1e-9), 1 - 1e-9)
    return FullEvaluation(
        parsed=parsed, group_evaluations=group_evals,
        overall_fair_probability=overall_p,
        overall_fair_american_odds=prob_to_american(overall_p),
        method=method,
    )
