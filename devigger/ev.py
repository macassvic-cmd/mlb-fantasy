"""
EV of a devigged fair probability against a flat pick'em payout - the
actual use case (comparing a fair-priced parlay/SGP/prop against a
pick'em book's flat multiplier, e.g. Betr/Underdog's "3 correct pays
5x").

CONVENTION: `payout_multiplier` is TOTAL RETURN per $1 staked if the
pick wins (matches how pick'em apps advertise it - "pays 5x" means a
$10 entry returns $50 total, i.e. $40 profit). EV_fraction below is
expected profit per $1 staked:

    EV_fraction = fair_probability * payout_multiplier - 1

Breakeven probability is 1 / payout_multiplier (the fair probability at
which EV_fraction is exactly 0). "Edge" is reported both ways - as a
probability-point difference (fair_probability - breakeven_probability)
and as the EV_fraction itself (expected return per dollar) - since the
two answer slightly different questions (how much extra hit-rate you
have vs. how much money you'd expect to make).
"""

from dataclasses import dataclass


@dataclass
class PickemEV:
    fair_probability: float
    payout_multiplier: float
    breakeven_probability: float
    edge_probability_points: float   # fair_probability - breakeven_probability
    ev_fraction: float               # expected profit per $1 staked
    clears_breakeven: bool


def pickem_ev(fair_probability: float, payout_multiplier: float) -> PickemEV:
    if not (0.0 < fair_probability < 1.0):
        raise ValueError(f"fair_probability must be in (0, 1), got {fair_probability}.")
    if payout_multiplier <= 1.0:
        raise ValueError(f"payout_multiplier must be > 1 (it's a TOTAL return multiple), got {payout_multiplier}.")

    breakeven = 1.0 / payout_multiplier
    ev_fraction = fair_probability * payout_multiplier - 1.0
    edge = fair_probability - breakeven

    return PickemEV(
        fair_probability=fair_probability,
        payout_multiplier=payout_multiplier,
        breakeven_probability=breakeven,
        edge_probability_points=edge,
        ev_fraction=ev_fraction,
        clears_breakeven=ev_fraction > 0.0,
    )
