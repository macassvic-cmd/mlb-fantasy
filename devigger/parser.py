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
    markets (player props)": no reference market given at all. See
    CALIBRATED against real CNM reference output below - this is no
    longer a flat-percentage guess.

CALIBRATION (2026-09-08): the original flat-hold-percentage guess for
"%" was validated against 4 real CNM reference legs (+100, -120, -165,
-110, all with CNM's own reported "default 10% juice" fair values) and
DIVERGED on every one of them, under every devig method available here
(multiplicative/additive/shin/power) - off anywhere from 0.1pp to 4pp,
worst for -110. No single (percentage, formula) combination reproduced
all 4 points, which rules out "wrong percentage" or "wrong formula" as
the fix - it means CNM's historical estimate isn't a closed-form
transform of the raw probability at all, almost certainly a fitted/
tabulated curve from real historical data this project doesn't have
access to. Reverse-engineering that curve from 4 algebra points isn't
possible with any confidence.

So instead of guessing a formula, synthesize_other_side_from_default_
juice now INTERPOLATES DIRECTLY through the known (raw_probability,
CNM_fair_probability) pairs - see _JUICE_CALIBRATION_POINTS. This
reproduces CNM's exact reference numbers within the calibrated range
(raw probability 0.5 to 0.6226, i.e. +100 through -165) when the
downstream method is multiplicative (the synthesized "other side" is
solved so that devig_multiplicative reproduces the calibrated target
exactly - see the algebra in synthesize_other_side_from_default_juice's
docstring). Two things this does NOT fix:
  - Only 4 calibration points exist, all in a narrow 50-62% band and
    all on the favorite side. Underdog primaries are handled by
    reflecting through 0.5 (assumes the same curve applies
    symmetrically to underdogs - unverified, just the most defensible
    option absent underdog reference data) and anything outside
    [0.5, 0.6226] is linearly EXTRAPOLATED from the nearest segment,
    clearly flagged in Market.juice_source as unverified when it
    happens.
  - -110's calibration point is itself an outlier relative to the other
    three (~4pp off from every formula tried, vs <1pp for the other
    three under their best-matching method) - included anyway, since
    there's no principled basis to exclude one of four real reference
    points, but flagged here in case it turns out to reflect a
    transcription issue rather than genuine CNM behavior.
  - Selecting a devig method OTHER than multiplicative for a "%" leg
    will NOT reproduce CNM's numbers - the calibration was solved
    specifically against multiplicative's formula. Untested how CNM's
    own tool behaves when a different method is selected for a
    "%"-juiced leg.
More reference points (especially outside 50-62% probability, and
ideally re-confirming -110) would let this calibration actually cover
the full odds range instead of extrapolating past ~62%/38%.

HONESTY CHECK (unchanged from before): the 3-value "alt-line" juice
bracket formula is still this project's own reasoned interpretation,
not verified against CNM's actual output. The 2-value "borrow total
hold from a reference market" case remains the most defensible of the
juice-spec paths (direct, minimal-assumption hold transfer). Every
synthesized leg records exactly how its missing side was derived in
Market.juice_source, and the /devig output surfaces that string so you
can sanity-check it against the real CNM tool.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional

from devig import american_to_prob, prob_to_american

# Calibration points from real CNM reference output (2026-09-06 test
# case: 5-leg parlay, "default 10% juice per leg"). (raw_probability,
# CNM's reported fair probability), sorted by raw_probability, favorite
# side only (raw >= 0.5) - see the CALIBRATION note above for how
# underdog primaries and out-of-range values are handled.
_JUICE_CALIBRATION_POINTS = [
    (100 / 200, 100 / 224),        # +100 -> CNM fair +124 (44.6429%)
    (110 / 210, 100 / 212),        # -110 -> CNM fair +112 (47.1698%) - see CALIBRATION note, this one's an outlier
    (120 / 220, 100 / 202),        # -120 -> CNM fair +102 (49.5050%)
    (165 / 265, 130 / 230),        # -165 -> CNM fair -130 (56.5217%)
]


def _interpolate_calibrated_fair_prob(raw_prob):
    """Piecewise-linear interpolation/extrapolation through
    _JUICE_CALIBRATION_POINTS. raw_prob < 0.5 (underdog primary) is
    handled by reflecting through 0.5 - see module docstring."""
    reflect = raw_prob < 0.5
    x = 1.0 - raw_prob if reflect else raw_prob

    pts = _JUICE_CALIBRATION_POINTS
    if x <= pts[0][0]:
        x0, y0 = pts[0]
        x1, y1 = pts[1]
    elif x >= pts[-1][0]:
        x0, y0 = pts[-2]
        x1, y1 = pts[-1]
    else:
        x0 = y0 = x1 = y1 = None
        for i in range(len(pts) - 1):
            if pts[i][0] <= x <= pts[i + 1][0]:
                x0, y0 = pts[i]
                x1, y1 = pts[i + 1]
                break

    frac = (x - x0) / (x1 - x0)
    y = y0 + frac * (y1 - y0)
    return 1.0 - y if reflect else y


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


def synthesize_other_side_from_default_juice(primary_odds):
    """Calibrated against real CNM reference output - see module
    docstring's CALIBRATION section. Looks up the target fair
    probability CNM would report for primary_odds via
    _interpolate_calibrated_fair_prob, then solves for the "other side"
    that makes devig_multiplicative reproduce that EXACT target:

        target = primary_raw / (primary_raw + other_raw)
        =>  other_raw = primary_raw * (1/target - 1)

    (algebraically exact for any primary_raw, not an approximation -
    verify by substitution: primary_raw / (primary_raw + primary_raw*
    (1/target - 1)) = primary_raw / (primary_raw * (1/target)) = target).
    Only exact when the downstream method is multiplicative - see
    module docstring for why other methods aren't verified for "%" legs.
    """
    primary_raw = american_to_prob(primary_odds)
    target_fair = _clamp_prob(_interpolate_calibrated_fair_prob(primary_raw))
    other_raw = _clamp_prob(primary_raw * (1.0 / target_fair - 1.0))
    other_odds = prob_to_american(other_raw)

    calibrated_lo = _JUICE_CALIBRATION_POINTS[0][0]
    calibrated_hi = _JUICE_CALIBRATION_POINTS[-1][0]
    query_x = primary_raw if primary_raw >= 0.5 else 1.0 - primary_raw
    in_range = calibrated_lo <= query_x <= calibrated_hi
    range_note = "" if in_range else " [EXTRAPOLATED beyond the calibrated 50-62% range - unverified]"

    source = (f"juice estimated via CNM-calibrated curve (target fair prob "
              f"{target_fair * 100:.2f}%, calibrated against real CNM reference "
              f"output - only exact with the multiplicative method){range_note}")
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
