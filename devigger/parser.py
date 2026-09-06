"""
CNM input-format parser. Grammar (informal):

    input       := or_group ("||" or_group)*
    or_group    := leg ("," leg)*
    leg         := market | juice_leg
    market      := side ("/" "^"? side)*          # first side = the one being wagered
    juice_leg   := number "/" "[" number (":" number)+ "]"
    juice_leg   := number "%"

Confirmed directly by the user against the real CNM spec (2026-09-06):
  - "," separates parlay legs (AND/multiplicative combination).
  - "/" alone separates the sides of ONE native market (2-way or N-way -
    e.g. +500/+250/+2000 is one 3-way market's three quoted sides).
  - "/^" (a "^" immediately after a "/") combines outcomes from
    SEPARATE 2-way markets into one synthetic mutually-exclusive
    multi-way (e.g. +500/^+800/^+1000 = three independent props' "yes"
    sides, combined as "exactly one of these three happens"). This
    doesn't need different MATH from a native multi-way market once the
    odds are collected into a list - devig.py operates on "a list of
    American odds for mutually exclusive outcomes" either way. The only
    difference is provenance/display, tracked via
    Market.combined_from_separate_markets.
  - "||" separates INDEPENDENT event groups combined via OR (at least
    one hitting wins, not all of them) - each side of "||" can itself be
    a full comma-separated parlay.
  - A bare single number with no "/" uses the fair-value shorthand:
    treated as already vig-free, with its exact negation as the assumed
    other side (American odds A and -A always imply probabilities
    summing to exactly 1.0).

JUICE SPECIFICATION (also confirmed against the CNM spec) - the
most-used feature for this tool's actual purpose, since pick'em books
only ever post one side of a market:
  - `+285/[-116:-106]` - "apply known juice from a similar market": a
    2-value bracket giving a complete reference market. Borrows that
    market's TOTAL HOLD and applies it directly to synthesize the
    primary leg's missing other side.
  - `-270/[-135:-110:-110]` - "derive from an alt-line market": a
    3-value bracket. Interpreted here as [alt_price, standard_side_a,
    standard_side_b] - the last two form a standard baseline market
    (its own hold = the baseline vig for this market family), the first
    is an alternate price point from the SAME family used to measure
    how much more hold gets loaded on as a price moves further from a
    coin flip (the documented favorite-longshot-bias tendency for vig to
    scale with how extreme a price is). That scaling factor is then
    applied to the primary leg based on ITS OWN distance from a coin
    flip, to produce a distance-scaled hold estimate.
  - `+600%` - "estimate juice historically for moderately juiced 2-way
    markets (player props)": no reference market given at all: applies
    DEFAULT_HISTORICAL_HOLD_PCT (a documented, adjustable estimate, NOT
    a verified CNM constant) as the assumed hold.

HONESTY CHECK: the "%"-default and the 3-value "alt-line" formulas are
my best-reasoned interpretation of the spec's own description, not
verified against CNM's actual output - there's no external academic
reference for either the way there is for Shin's method. The 2-value
"borrow total hold from a reference market" case is the most defensible
of the three (direct, minimal-assumption hold transfer). Every
synthesized leg records exactly how its missing side was derived in
Market.juice_source, and the /devig output will surface that string so
you can sanity-check it against the real CNM tool per your own request.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

from devig import american_to_prob, prob_to_american

# Documented estimate for a "moderately juiced" 2-way player-prop market -
# NOT a verified CNM constant. Override via synthesize_other_side_from_default_juice's
# hold_pct argument, or the /devig command's own override, once wired up.
DEFAULT_HISTORICAL_HOLD_PCT = 4.5


@dataclass
class Market:
    odds: List[float]
    combined_from_separate_markets: bool = False
    juice_source: Optional[str] = None


@dataclass
class ParsedLeg:
    market: Market
    raw_text: str = ""


@dataclass
class ParlayGroup:
    legs: List[ParsedLeg] = field(default_factory=list)


@dataclass
class ParsedInput:
    or_groups: List[ParlayGroup] = field(default_factory=list)


_JUICE_BRACKET_LEG_RE = re.compile(r'^([+-]\d+)/\[([^\]]+)\]$')
_JUICE_PERCENT_LEG_RE = re.compile(r'^([+-]\d+)%$')
_SIDE_RE = re.compile(r'(\^?)([+-]\d+)')


def _clamp_prob(p):
    return min(max(p, 1e-6), 1 - 1e-6)


def _apply_hold(primary_raw, hold):
    """other_raw = 1 + hold - primary_raw, with a hard safety cap: the
    synthesized other side must land on the OPPOSITE side of a coin flip
    from the primary leg (a favorite's missing side must synthesize to
    an underdog, and vice versa) - that's a structural requirement for
    representing a real two-outcome market at all, not a judgment call.
    A large enough hold estimate can otherwise push other_raw past 0.5
    in the same direction as primary_raw (both sides reading as
    "favorites"), which happened for the 3-value alt-line heuristic on
    a real example (-270 favorite + a large extremity-scaled hold) -
    caught by test_parser.py, capped here rather than trusting an
    unbounded scale factor."""
    if primary_raw > 0.5:
        max_hold = (primary_raw - 0.5) * 0.98  # leave a buffer so other_raw stays strictly < 0.5
        hold = min(hold, max_hold)
    return 1.0 + hold - primary_raw


def synthesize_other_side_from_reference(primary_odds, ref_odds_list):
    """See module docstring - 2-value and 3-value bracket handling."""
    primary_raw = american_to_prob(primary_odds)

    if len(ref_odds_list) == 2:
        ref_raw = [american_to_prob(o) for o in ref_odds_list]
        ref_hold = sum(ref_raw) - 1.0
        other_raw = _apply_hold(primary_raw, ref_hold)
        source = (f"juice borrowed from reference market {ref_odds_list} "
                   f"(hold={ref_hold * 100:.2f}%)")
    elif len(ref_odds_list) == 3:
        alt_price, std_a, std_b = ref_odds_list
        std_raw = [american_to_prob(std_a), american_to_prob(std_b)]
        std_hold = sum(std_raw) - 1.0
        std_avg_extremity = abs((sum(std_raw) / 2) - 0.5)
        alt_extremity = abs(american_to_prob(alt_price) - 0.5)
        primary_extremity = abs(primary_raw - 0.5)

        if alt_extremity > std_avg_extremity + 1e-9:
            scale = 1.0 + (primary_extremity - std_avg_extremity) / (alt_extremity - std_avg_extremity)
        else:
            scale = 1.0
        scale = max(scale, 0.1)  # floor - never scale hold to near-zero or negative
        scaled_hold = std_hold * scale
        other_raw = _apply_hold(primary_raw, scaled_hold)
        source = (f"juice derived from alt-line reference {ref_odds_list} "
                   f"(baseline hold={std_hold * 100:.2f}%, scaled x{scale:.2f} "
                   f"for this leg's price extremity -> {scaled_hold * 100:.2f}%, "
                   f"capped if it would flip the other side's favorite/underdog direction)")
    else:
        raise ValueError(f"Juice reference bracket must have 2 or 3 values, got {len(ref_odds_list)}: {ref_odds_list}")

    other_odds = prob_to_american(_clamp_prob(other_raw))
    return other_odds, source


def synthesize_other_side_from_default_juice(primary_odds, hold_pct=DEFAULT_HISTORICAL_HOLD_PCT):
    primary_raw = american_to_prob(primary_odds)
    hold = hold_pct / 100.0
    other_raw = _apply_hold(primary_raw, hold)
    other_odds = prob_to_american(_clamp_prob(other_raw))
    source = (f"juice estimated via default historical hold ({hold_pct:.1f}%, "
              "moderately-juiced 2-way player-prop estimate - NOT a verified CNM constant)")
    return other_odds, source


def parse_leg(leg_str):
    leg_str = leg_str.strip()
    if not leg_str:
        raise ValueError("Empty leg.")

    m = _JUICE_BRACKET_LEG_RE.match(leg_str)
    if m:
        primary = int(m.group(1))
        refs = [int(x) for x in m.group(2).split(':')]
        other, source = synthesize_other_side_from_reference(primary, refs)
        return ParsedLeg(Market(odds=[primary, other], juice_source=source), raw_text=leg_str)

    m = _JUICE_PERCENT_LEG_RE.match(leg_str)
    if m:
        primary = int(m.group(1))
        other, source = synthesize_other_side_from_default_juice(primary)
        return ParsedLeg(Market(odds=[primary, other], juice_source=source), raw_text=leg_str)

    sides = _SIDE_RE.findall(leg_str)
    if not sides:
        raise ValueError(f"Could not parse leg: {leg_str!r}")
    numbers = [int(n) for _, n in sides]
    is_xor = any(caret == '^' for caret, _ in sides)

    if len(numbers) == 1:
        primary = numbers[0]
        other = -primary
        return ParsedLeg(
            Market(odds=[primary, other], juice_source="fair_value_shorthand (assumed no vig - mirrored other side)"),
            raw_text=leg_str,
        )

    return ParsedLeg(Market(odds=numbers, combined_from_separate_markets=is_xor), raw_text=leg_str)


def parse_input(input_str):
    input_str = input_str.strip()
    if not input_str:
        raise ValueError("Empty input.")

    or_group_strs = input_str.split('||')
    or_groups = []
    for group_str in or_group_strs:
        leg_strs = [s.strip() for s in group_str.split(',') if s.strip()]
        if not leg_strs:
            raise ValueError(f"Empty OR-group in input: {input_str!r}")
        legs = [parse_leg(s) for s in leg_strs]
        or_groups.append(ParlayGroup(legs=legs))
    return ParsedInput(or_groups=or_groups)
