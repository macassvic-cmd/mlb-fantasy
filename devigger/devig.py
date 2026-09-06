"""
Devigging math for the Crazy Ninja Mike (CNM)-style devigger.

Scope note (2026-09-06): this module implements the four devig methods
against a list of American odds for ONE market (2-way or N-way) and is
built/tested BEFORE the full CNM input-format parser (comma/slash/^/||
grammar) or the Discord bot, per explicit instruction to verify the math
first. Two things in the user's own spec transcription were ambiguous
enough that I did not want to guess and build on top of a wrong
assumption:
  1. The "||" OR-operator example ("-115/-110,-185/+140 with || for OR
     operators") doesn't actually contain a "||" character, so I can't
     tell if that's illustrative prose or a transcription gap.
  2. Plain multi-way ("+500/+250/+2000") vs "^"-marked XOR multi-way
     ("+500/^+800/^+1000") - unclear what distinguishes them, since a
     plain N-way market's outcomes are already mutually exclusive by
     definition, which is what "^" would seem to flag.
These only affect the PARSER (comma/slash/^/|| grammar), not the devig
math itself - a market is just "a list of American odds for mutually
exclusive outcomes" regardless of how it's spelled in CNM's string
format, so building and testing this module doesn't require resolving
them. Flagged for the next phase (parser + parlay/SGP combination).

METHODS:
  1. multiplicative - divide each raw implied probability by the sum
     (the standard "proportional"/basic devig, CNM's default).
  2. additive (Shin's method) - see devig_shin()'s docstring for the
     exact formula and why "additive" and "Shin" are implemented as the
     same slot here (the user's own spec lists them together:
     "Additive/Shin"). A plain linear-additive fallback
     (devig_additive_linear) is also provided since it's a distinct,
     simpler, well-defined method in its own right, in case CNM's
     "Additive" turns out to mean that instead once reference examples
     are available.
  3. power - find the exponent k solving sum(pi^k) = 1.
  4. worst_case - per the user's own explicit definition ("highest juice
     on the longest leg"): the single longest-odds (lowest raw
     probability) outcome absorbs 100% of the market's hold; every other
     outcome's fair probability equals its raw quoted probability
     unchanged. This one has no external-spec ambiguity - implemented
     directly from that description.

VERIFICATION STATUS: multiplicative, additive_linear, power, and
worst_case are standard closed-form/well-defined methods with no room
for interpretation - verified here against hand-computable and
invariant-based unit tests (test_devig.py). Shin's method uses the
formula from Shin (1993) as commonly implemented in the sports-betting
devigging community (see devig_shin() docstring) - I do not have the
actual CNM docs to confirm this matches their exact implementation
bit-for-bit. If you have specific CNM reference examples (input odds ->
expected fair probabilities per method), paste them and I'll verify/
adjust against real ground truth rather than just internal consistency.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class DevigResult:
    method: str
    raw_probabilities: List[float]      # implied probabilities before devig, one per outcome
    fair_probabilities: List[float]      # devigged probabilities, sums to 1.0
    hold_pct: float                      # (sum(raw_probabilities) - 1) * 100
    fair_american_odds: List[float] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.fair_american_odds:
            self.fair_american_odds = [prob_to_american(p) for p in self.fair_probabilities]


# ---------------------------------------------------------------------------
# American <-> probability conversion
# ---------------------------------------------------------------------------

def american_to_prob(odds):
    """American odds (e.g. +150, -200) -> implied probability (0, 1)."""
    if odds == 0:
        raise ValueError("American odds cannot be 0.")
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def prob_to_american(prob):
    """Probability (0, 1) -> fair American odds. 0.5 exactly is a pick'em,
    returned as -100 by convention (a fair coin-flip has no vig either way;
    -100 is the standard way sportsbooks + devigging tools express that,
    not +100 - both are equally "correct" at exactly 0.5, this just picks
    one so the function is total)."""
    if not (0.0 < prob < 1.0):
        raise ValueError(f"Probability must be strictly between 0 and 1, got {prob}.")
    if prob >= 0.5:
        return -100.0 * prob / (1.0 - prob)
    return 100.0 * (1.0 - prob) / prob


def raw_probabilities(odds_list):
    return [american_to_prob(o) for o in odds_list]


def _hold_pct(raw_probs):
    return (sum(raw_probs) - 1.0) * 100.0


# ---------------------------------------------------------------------------
# 1. Multiplicative
# ---------------------------------------------------------------------------

def devig_multiplicative(odds_list):
    """Standard proportional devig: fair_p_i = raw_p_i / sum(raw_p)."""
    raw = raw_probabilities(odds_list)
    total = sum(raw)
    fair = [p / total for p in raw]
    return DevigResult(method="multiplicative", raw_probabilities=raw,
                        fair_probabilities=fair, hold_pct=_hold_pct(raw))


# ---------------------------------------------------------------------------
# 2a. Additive (plain linear) - see module docstring for why this is kept
# as a separate function from devig_shin.
# ---------------------------------------------------------------------------

def devig_additive_linear(odds_list):
    """fair_p_i = raw_p_i - (sum(raw_p) - 1) / n - subtracts an equal
    absolute share of the total hold from every outcome. Can drive a
    probability below 0 for a very short-priced favorite in a market with
    unusually high hold and few outcomes (e.g. odds-on favorite in a
    2-way market with 15%+ hold) - clamped to a small epsilon with a
    warning rather than returning a negative/zero probability, since
    that's not a meaningful fair price."""
    raw = raw_probabilities(odds_list)
    n = len(raw)
    excess = sum(raw) - 1.0
    share = excess / n
    fair = [p - share for p in raw]
    warnings = []
    EPS = 1e-6
    if any(p <= 0 for p in fair):
        warnings.append(
            "Additive method drove at least one outcome's probability to/below 0 "
            "(short-priced favorite + high hold + few outcomes) - clamped to a small "
            "epsilon. Consider multiplicative or Shin for this market instead."
        )
        fair = [max(p, EPS) for p in fair]
        total = sum(fair)
        fair = [p / total for p in fair]  # renormalize after clamping
    return DevigResult(method="additive_linear", raw_probabilities=raw,
                        fair_probabilities=fair, hold_pct=_hold_pct(raw), warnings=warnings)


# ---------------------------------------------------------------------------
# 2b. Shin's method (the "Additive/Shin" slot in the CNM method list)
# ---------------------------------------------------------------------------

def devig_shin(odds_list, tol=1e-12, max_iter=200):
    """Shin (1993): models the bookmaker's overround as compensation for
    the risk of trading against better-informed ("insider") bettors,
    parameterized by z = the implied proportion of insider money. For raw
    implied probabilities pi_i (summing to Sigma > 1), z solves:

        p_i(z) = [ sqrt(z^2 + 4*(1-z)*pi_i^2/Sigma) - z ] / (2*(1-z))
        sum_i p_i(z) = 1

    Solved here via bisection on z in [0, 1) (p_i(z) is monotonic in z
    for fixed pi_i, so sum_i p_i(z) is monotonic in z, guaranteeing a
    unique root). At z=0, this collapses to p_i = pi_i / sqrt(Sigma); as
    z increases the adjustment reshapes towards favoring favorites less
    and underdogs more than straight multiplicative division does - the
    qualitative behavior devigging literature attributes to Shin's method.

    z=0 exactly recovers a scaled-by-sqrt(Sigma) result, not the
    multiplicative result - the two methods only coincide exactly when
    Sigma=1 (no vig to begin with, tested as a cross-method invariant in
    test_devig.py), which is expected: they're different models of WHERE
    the vig comes from, not two ways of computing the same thing.

    NOT verified against CNM's actual implementation - see module
    docstring. This is the standard academic formula as commonly
    implemented for sports-betting devigging; if CNM's "Shin" produces
    different numbers, share a reference example and I'll reconcile.
    """
    raw = raw_probabilities(odds_list)
    sigma = sum(raw)

    def probs_for_z(z):
        if z <= 0.0:
            return [p / (sigma ** 0.5) for p in raw]
        return [
            (((z ** 2 + 4 * (1 - z) * (p ** 2) / sigma) ** 0.5) - z) / (2 * (1 - z))
            for p in raw
        ]

    warnings = []
    if sigma <= 1.0:
        # No vig (or a mispriced/arbitrage-free-already market) - z=0 is
        # the only sensible root; probs_for_z(0) already sums to 1 in
        # this case (sigma=1) or there's no valid z solving the equation
        # (sigma<1, a "negative vig" market that shouldn't occur from
        # real two-sided quotes) - fall back to the raw probabilities
        # renormalized rather than bisecting on a sign that never flips.
        fair = raw if abs(sigma - 1.0) < 1e-9 else [p / sigma for p in raw]
        if sigma < 1.0 - 1e-9:
            warnings.append(
                f"Sum of raw implied probabilities ({sigma:.4f}) is below 1 - this isn't "
                "a real overround market (arbitrage or bad input). Shin's model doesn't "
                "apply; returned a simple renormalization instead."
            )
        return DevigResult(method="shin", raw_probabilities=raw, fair_probabilities=fair,
                            hold_pct=_hold_pct(raw), warnings=warnings)

    # BUG FIXED 2026-09-06: this used to short-circuit here whenever
    # sum(probs_for_z(0)) > 1 ("z=0 is the best available answer") -
    # but that sum equals sqrt(sigma), which is >1 for EVERY sigma>1,
    # i.e. every normal vigged market. That's not a rare edge case, it's
    # the expected starting condition for the bisection below (f_lo>0,
    # f_hi<0 is exactly what a valid bracket looks like) - the shortcut
    # was hijacking essentially all real inputs before the actual
    # root-search ever ran, returning z=0's numbers as if they solved
    # the equation when they generally don't. Caught by test_devig.py's
    # "does the returned z actually solve Shin's own defining equation"
    # check - it does now; it silently didn't before.
    lo, hi = 0.0, 1.0 - 1e-9
    z = 0.5
    for _ in range(max_iter):
        total = sum(probs_for_z(z)) - 1.0
        if abs(total) < tol:
            break
        if total > 0:
            lo = z
        else:
            hi = z
        z = (lo + hi) / 2.0

    fair = probs_for_z(z)
    total = sum(fair)
    fair = [p / total for p in fair]  # guard against residual float drift
    return DevigResult(method="shin", raw_probabilities=raw, fair_probabilities=fair,
                        hold_pct=_hold_pct(raw), warnings=warnings)


# ---------------------------------------------------------------------------
# 3. Power method
# ---------------------------------------------------------------------------

def devig_power(odds_list, tol=1e-12, max_iter=200):
    """Find k such that sum(raw_p_i ^ k) = 1, then fair_p_i = raw_p_i^k.
    sum(p_i^k) is strictly decreasing in k for k>0 (each p_i in (0,1)),
    so bisection on k has a unique root and is guaranteed to converge."""
    raw = raw_probabilities(odds_list)

    def total_for_k(k):
        return sum(p ** k for p in raw)

    # total_for_k(k) is strictly decreasing in k over ALL real k (each
    # p_i in (0,1), so a higher power always shrinks it) - covers k<1 too,
    # needed for a sum(raw)<1 input (e.g. all-underdog odds with no real
    # vig), where the root lies BELOW k=1, not above it. BUG FIXED
    # 2026-09-06: this used to only expand hi upward, so a sum(raw)<1
    # input left lo=hi=1.0 with the loop never executing, converging to
    # k=1 - which does not solve sum(p_i^k)=1 unless sigma already was 1.
    lo, hi = 1.0, 1.0
    if total_for_k(1.0) > 1.0:
        while total_for_k(hi) > 1.0:
            hi *= 2.0
            if hi > 1e6:
                break  # pathological input guard - shouldn't happen for real odds
    else:
        while total_for_k(lo) < 1.0:
            lo /= 2.0
            if lo < 1e-6:
                break

    k = hi
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        t = total_for_k(mid)
        if abs(t - 1.0) < tol:
            k = mid
            break
        if t > 1.0:
            lo = mid
        else:
            hi = mid
        k = mid

    fair = [p ** k for p in raw]
    total = sum(fair)
    fair = [p / total for p in fair]  # guard against residual float drift
    return DevigResult(method="power", raw_probabilities=raw, fair_probabilities=fair,
                        hold_pct=_hold_pct(raw))


# ---------------------------------------------------------------------------
# 4. Worst-case
# ---------------------------------------------------------------------------

def devig_worst_case(odds_list):
    """Per the user's own spec: 'highest juice on the longest leg' - the
    single longest-odds (lowest raw probability) outcome absorbs the
    ENTIRE market hold; every other outcome keeps its raw quoted
    probability unchanged. This is the most conservative fair-value
    estimate for every outcome EXCEPT the longest one (whose true
    probability this method assumes is being systematically overstated
    the most by the vig), so it's the right lens for "what's the worst
    realistic fair price I should assume for the side I'm betting,"
    which is the stated use case."""
    raw = raw_probabilities(odds_list)
    excess = sum(raw) - 1.0
    longest_idx = min(range(len(raw)), key=lambda i: raw[i])  # lowest prob = longest odds

    fair = list(raw)
    fair[longest_idx] = raw[longest_idx] - excess

    warnings = []
    if fair[longest_idx] <= 0:
        warnings.append(
            "Worst-case method: the longest leg's raw probability can't absorb the "
            "entire market hold on its own (would go to/below 0) - clamped to a small "
            "epsilon. This market's hold is unusually large relative to its longest "
            "price; treat this method's output with extra caution here."
        )
        fair[longest_idx] = 1e-6
        total = sum(fair)
        fair = [p / total for p in fair]

    return DevigResult(method="worst_case", raw_probabilities=raw, fair_probabilities=fair,
                        hold_pct=_hold_pct(raw), warnings=warnings)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

METHODS = {
    "multiplicative": devig_multiplicative,
    "additive": devig_shin,          # see module docstring: "Additive/Shin" is one slot
    "additive_linear": devig_additive_linear,
    "shin": devig_shin,
    "power": devig_power,
    "worst_case": devig_worst_case,
}


def devig(odds_list, method="multiplicative"):
    if method not in METHODS:
        raise ValueError(f"Unknown devig method {method!r}. Choose from: {sorted(METHODS)}")
    if len(odds_list) < 2:
        raise ValueError("A market needs at least 2 outcomes to devig.")
    return METHODS[method](odds_list)
