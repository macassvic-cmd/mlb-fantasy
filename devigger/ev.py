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
from typing import Optional


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


@dataclass
class KellySizing:
    fair_probability: float
    payout_multiplier: float
    # Fractions of bankroll (e.g. 0.0224 = 2.24% of bankroll) - this IS
    # what CNM's "u" turned out to mean, see module docstring below.
    full_kelly_fraction: float
    half_kelly_fraction: float
    quarter_kelly_fraction: float
    bankroll: Optional[float] = None
    full_kelly_dollars: Optional[float] = None
    half_kelly_dollars: Optional[float] = None
    quarter_kelly_dollars: Optional[float] = None


def kelly_sizing(fair_probability: float, payout_multiplier: float, bankroll: float = None) -> KellySizing:
    """Standard Kelly criterion: f* = (b*p - q) / b, where b = net odds
    (payout_multiplier - 1, since payout_multiplier is TOTAL return
    including stake - see this module's own top docstring) and
    q = 1 - p. Negative Kelly (a -EV bet) clamps to 0 - Kelly sizing
    doesn't recommend laying off a bet you don't have, it recommends not
    betting.

    UNIT CONVENTION - VALIDATED (2026-09-08), not guessed: CNM's
    reference output for the 5-leg parlay example (correlated fair
    probability ~5.6%, offered +2800/29.0x) reported "Kelly $5.60 (Full
    2.24u, 1/2 1.12u, 1/4 0.56u)". Computing f* here from that same
    (p, multiplier) pair gives ~2.24-2.26% (the precise value depends on
    which rounding of p is used, all land within ~0.02pp of CNM's 2.24)
    - confirming "u" means PERCENTAGE POINTS OF BANKROLL (1u = 1% of
    bankroll), not a fixed dollar unit. That's the standard way Kelly
    sizing is normally expressed anyway, so full_kelly_fraction etc. are
    reported as fractions of bankroll by default. Dollar amounts are
    only computed if you supply your own `bankroll` - CNM's own $5.60
    implies a ~$250 bankroll (or equivalently $2.50/unit) for THAT one
    example, but nothing here assumes any particular bankroll unless
    you provide one.
    """
    if not (0.0 < fair_probability < 1.0):
        raise ValueError(f"fair_probability must be in (0, 1), got {fair_probability}.")
    if payout_multiplier <= 1.0:
        raise ValueError(f"payout_multiplier must be > 1 (it's a TOTAL return multiple), got {payout_multiplier}.")

    b = payout_multiplier - 1.0
    q = 1.0 - fair_probability
    full = (b * fair_probability - q) / b
    full = max(full, 0.0)

    result = KellySizing(
        fair_probability=fair_probability, payout_multiplier=payout_multiplier,
        full_kelly_fraction=full, half_kelly_fraction=full / 2.0, quarter_kelly_fraction=full / 4.0,
        bankroll=bankroll,
    )
    if bankroll is not None:
        result.full_kelly_dollars = full * bankroll
        result.half_kelly_dollars = result.half_kelly_fraction * bankroll
        result.quarter_kelly_dollars = result.quarter_kelly_fraction * bankroll
    return result
