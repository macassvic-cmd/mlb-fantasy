"""
Unit tests for devig.py. Run with: python -m unittest test_devig -v

Three tiers of confidence, deliberately kept distinct (see devig.py's
module docstring for why):
  - Hand-computed exact values: multiplicative, additive_linear,
    worst_case, and the odds<->probability conversions - simple enough
    arithmetic to verify by hand independently of the implementation.
  - Cross-method invariants that must hold regardless of which method's
    formula is "right": all four methods agree exactly when there's no
    vig to remove; every method's fair probabilities sum to 1; every
    fair probability stays in (0, 1).
  - Shin and power: verified against their OWN defining equations
    (does the returned z/k actually solve sum(p_i(z))=1 or sum(p_i^k)=1)
    since neither has a simple hand-computable closed form for arbitrary
    odds - this confirms the root-finder is internally correct, not that
    the formula matches CNM's specific implementation (unverified - see
    devig.py's module docstring).
"""

import math
import unittest

from devig import (
    american_to_prob, prob_to_american, devig,
    devig_multiplicative, devig_additive_linear, devig_shin,
    devig_power, devig_worst_case,
)


class TestOddsConversion(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(american_to_prob(100), 0.5)
        self.assertAlmostEqual(american_to_prob(-100), 0.5)
        self.assertAlmostEqual(american_to_prob(200), 1 / 3)
        self.assertAlmostEqual(american_to_prob(-200), 2 / 3)
        self.assertAlmostEqual(american_to_prob(-110), 110 / 210)
        self.assertAlmostEqual(american_to_prob(150), 100 / 250)

    def test_zero_odds_rejected(self):
        with self.assertRaises(ValueError):
            american_to_prob(0)

    def test_prob_to_american_known_values(self):
        self.assertAlmostEqual(prob_to_american(1 / 3), 200, places=6)
        self.assertAlmostEqual(prob_to_american(2 / 3), -200, places=6)
        self.assertAlmostEqual(prob_to_american(0.5), -100, places=6)

    def test_round_trip(self):
        # +100 deliberately excluded: it maps to exactly p=0.5, and
        # prob_to_american(0.5) returns -100 by documented convention
        # (see its docstring) - not a round-trip bug, just the one point
        # where +100 and -100 are equally "correct" and the function has
        # to pick one.
        for odds in [-500, -300, -170, -110, 110, 150, 300, 500, 2000]:
            prob = american_to_prob(odds)
            back = prob_to_american(prob)
            self.assertAlmostEqual(odds, back, places=4)

    def test_prob_bounds_rejected(self):
        with self.assertRaises(ValueError):
            prob_to_american(0.0)
        with self.assertRaises(ValueError):
            prob_to_american(1.0)
        with self.assertRaises(ValueError):
            prob_to_american(1.5)


class TestNoVigInvariant(unittest.TestCase):
    """When raw implied probabilities already sum to exactly 1 (no vig),
    every devig method must return the raw probabilities unchanged - all
    four are trying to answer "what's the vig-free probability," and
    there's nothing to remove."""

    def test_two_way_no_vig(self):
        # +100/-100 has NO vig: 0.5 + 0.5 = 1.0 exactly.
        odds = [100, -100]
        for name, fn in [("multiplicative", devig_multiplicative),
                          ("additive_linear", devig_additive_linear),
                          ("shin", devig_shin),
                          ("power", devig_power),
                          ("worst_case", devig_worst_case)]:
            result = fn(odds)
            self.assertAlmostEqual(result.fair_probabilities[0], 0.5, places=6, msg=name)
            self.assertAlmostEqual(result.fair_probabilities[1], 0.5, places=6, msg=name)
            self.assertAlmostEqual(result.hold_pct, 0.0, places=6, msg=name)

    def test_three_way_no_vig(self):
        # +200/+200/+200 -> each raw prob = 1/3, sums to exactly 1.0.
        odds = [200, 200, 200]
        for name, fn in [("multiplicative", devig_multiplicative),
                          ("additive_linear", devig_additive_linear),
                          ("shin", devig_shin),
                          ("power", devig_power),
                          ("worst_case", devig_worst_case)]:
            result = fn(odds)
            for p in result.fair_probabilities:
                self.assertAlmostEqual(p, 1 / 3, places=6, msg=name)


class TestSymmetricMarketsGiveEvenSplit(unittest.TestCase):
    """Any market where all outcomes carry identical odds must devig to
    an exactly even split, for multiplicative/additive/shin/power -
    each of those treats every outcome by the same formula applied
    uniformly, so identical inputs must produce identical outputs.

    worst_case is deliberately EXCLUDED here: by design it always singles
    out exactly one outcome (the longest odds) to absorb 100% of the
    hold. When every outcome is tied for "longest," it still has to pick
    one (Python's min() takes the first tie), which necessarily breaks
    symmetry - that's not a bug, it's the method doing what it says on
    the tin. See TestWorstCase for its own correctness tests instead."""

    METHODS = [("multiplicative", devig_multiplicative),
               ("additive_linear", devig_additive_linear),
               ("shin", devig_shin),
               ("power", devig_power)]

    def test_minus_110_both_sides(self):
        odds = [-110, -110]
        for name, fn in self.METHODS:
            result = fn(odds)
            self.assertAlmostEqual(result.fair_probabilities[0], 0.5, places=6, msg=name)
            self.assertAlmostEqual(result.fair_probabilities[1], 0.5, places=6, msg=name)

    def test_three_way_symmetric(self):
        odds = [150, 150, 150]
        for name, fn in self.METHODS:
            result = fn(odds)
            for p in result.fair_probabilities:
                self.assertAlmostEqual(p, 1 / 3, places=6, msg=name)


class TestMultiplicative(unittest.TestCase):
    def test_minus_110_hold(self):
        result = devig_multiplicative([-110, -110])
        raw = 110 / 210
        expected_hold = (2 * raw - 1) * 100
        self.assertAlmostEqual(result.hold_pct, expected_hold, places=6)
        self.assertAlmostEqual(result.hold_pct, 4.761905, places=4)

    def test_asymmetric_hand_computed(self):
        # +150/-170: raw probs 0.4 and 170/270, hand-computed fair split.
        odds = [150, -170]
        raw0 = 100 / 250          # 0.4
        raw1 = 170 / 270          # 0.62962963
        total = raw0 + raw1       # 1.02962963
        expected0 = raw0 / total
        expected1 = raw1 / total

        result = devig_multiplicative(odds)
        self.assertAlmostEqual(result.raw_probabilities[0], raw0, places=8)
        self.assertAlmostEqual(result.raw_probabilities[1], raw1, places=8)
        self.assertAlmostEqual(result.fair_probabilities[0], expected0, places=8)
        self.assertAlmostEqual(result.fair_probabilities[1], expected1, places=8)
        self.assertAlmostEqual(sum(result.fair_probabilities), 1.0, places=8)

    def test_three_way_hand_computed(self):
        # +500/+250/+2000
        odds = [500, 250, 2000]
        raw = [100 / 600, 100 / 350, 100 / 2100]
        total = sum(raw)
        expected = [r / total for r in raw]

        result = devig_multiplicative(odds)
        for got, want in zip(result.fair_probabilities, expected):
            self.assertAlmostEqual(got, want, places=8)
        self.assertAlmostEqual(sum(result.fair_probabilities), 1.0, places=8)


class TestAdditiveLinear(unittest.TestCase):
    def test_asymmetric_hand_computed(self):
        # +150/-170, hand-computed via the additive formula independently
        # of the implementation: fair_i = raw_i - (sum(raw) - 1) / n
        odds = [150, -170]
        raw0 = 100 / 250
        raw1 = 170 / 270
        excess = (raw0 + raw1) - 1.0
        share = excess / 2
        expected0 = raw0 - share
        expected1 = raw1 - share

        result = devig_additive_linear(odds)
        self.assertAlmostEqual(result.fair_probabilities[0], expected0, places=8)
        self.assertAlmostEqual(result.fair_probabilities[1], expected1, places=8)
        self.assertAlmostEqual(sum(result.fair_probabilities), 1.0, places=8)

    def test_differs_from_multiplicative_when_asymmetric(self):
        odds = [150, -170]
        mult = devig_multiplicative(odds)
        add = devig_additive_linear(odds)
        # Both must sum to 1, but should NOT produce identical splits for
        # an asymmetric market - if they do, one of the two implementations
        # has collapsed into the other by mistake.
        self.assertAlmostEqual(sum(mult.fair_probabilities), 1.0, places=8)
        self.assertAlmostEqual(sum(add.fair_probabilities), 1.0, places=8)
        self.assertNotAlmostEqual(mult.fair_probabilities[0], add.fair_probabilities[0], places=4)

    def test_negative_probability_clamped_with_warning(self):
        # A 2-way market can NEVER trigger this clamp: for n=2, the
        # clamp condition is share > raw_min, i.e.
        # (raw0+raw1-1)/2 > min(raw0,raw1) - algebraically this reduces
        # to requiring one raw probability to be negative, which can't
        # happen for real odds. Needs n>=3 with two heavy favorites
        # (large raw share) crammed against one huge underdog (tiny raw
        # probability that the shared excess/n easily exceeds).
        odds = [-10000, -10000, 100000]
        result = devig_additive_linear(odds)
        self.assertTrue(len(result.warnings) >= 1)
        self.assertAlmostEqual(sum(result.fair_probabilities), 1.0, places=6)
        for p in result.fair_probabilities:
            self.assertGreater(p, 0.0)


class TestWorstCase(unittest.TestCase):
    def test_two_way_hand_computed(self):
        # +150/-170: longest leg is +150 (raw prob 0.4, the lower one).
        # It absorbs the entire hold; -170 stays at its raw probability.
        odds = [150, -170]
        raw0 = 100 / 250           # 0.4 - longest leg (lower prob)
        raw1 = 170 / 270
        excess = (raw0 + raw1) - 1.0
        expected0 = raw0 - excess  # longest leg absorbs everything
        expected1 = raw1           # unchanged

        result = devig_worst_case(odds)
        self.assertAlmostEqual(result.fair_probabilities[0], expected0, places=8)
        self.assertAlmostEqual(result.fair_probabilities[1], expected1, places=8)
        self.assertAlmostEqual(sum(result.fair_probabilities), 1.0, places=8)

    def test_three_way_only_longest_leg_moves(self):
        # +500/+250/+2000: longest leg is +2000 (lowest raw probability).
        odds = [500, 250, 2000]
        raw = [100 / 600, 100 / 350, 100 / 2100]
        excess = sum(raw) - 1.0
        longest_idx = raw.index(min(raw))
        expected = list(raw)
        expected[longest_idx] -= excess

        result = devig_worst_case(odds)
        for i, (got, want) in enumerate(zip(result.fair_probabilities, expected)):
            self.assertAlmostEqual(got, want, places=8, msg=f"index {i}")
        # Confirm the other two legs were genuinely untouched.
        for i in range(3):
            if i != longest_idx:
                self.assertAlmostEqual(result.fair_probabilities[i], raw[i], places=8)

    def test_pathological_hold_clamped(self):
        # Longest leg's raw probability can't absorb the full hold alone -
        # same fixture as TestAdditiveLinear's clamp test: two heavy
        # favorites' combined excess dwarfs the huge underdog's tiny raw
        # probability.
        odds = [-10000, -10000, 100000]
        result = devig_worst_case(odds)
        self.assertTrue(len(result.warnings) >= 1)
        self.assertAlmostEqual(sum(result.fair_probabilities), 1.0, places=6)
        for p in result.fair_probabilities:
            self.assertGreater(p, 0.0)


class TestShin(unittest.TestCase):
    def test_sums_to_one(self):
        for odds in ([150, -170], [-110, -110], [500, 250, 2000], [-200, 150, 800]):
            result = devig_shin(odds)
            self.assertAlmostEqual(sum(result.fair_probabilities), 1.0, places=6, msg=odds)
            for p in result.fair_probabilities:
                self.assertGreater(p, 0.0, msg=odds)
                self.assertLess(p, 1.0, msg=odds)

    def test_root_actually_solves_defining_equation(self):
        """Independently re-derive z from the result and confirm
        plugging it back into Shin's own equation reproduces sum=1 -
        this validates the bisection search is internally correct,
        not that the formula matches any external reference."""
        odds = [150, -170]
        raw = [american_to_prob(o) for o in odds]
        sigma = sum(raw)
        result = devig_shin(odds)
        p0, p1 = result.fair_probabilities

        # Recover z from p0's defining equation: p = (sqrt(z^2+4(1-z)pi^2/S)-z)/(2(1-z))
        # Solve numerically for z given p0, pi0, sigma, then check p1 too.
        def p_for_z(z, pi):
            if z <= 0:
                return pi / math.sqrt(sigma)
            return (math.sqrt(z ** 2 + 4 * (1 - z) * pi ** 2 / sigma) - z) / (2 * (1 - z))

        lo, hi = 0.0, 1.0 - 1e-9
        for _ in range(100):
            mid = (lo + hi) / 2
            if p_for_z(mid, raw[0]) > p0:
                lo = mid
            else:
                hi = mid
        z_recovered = (lo + hi) / 2
        self.assertAlmostEqual(p_for_z(z_recovered, raw[1]), p1, places=4)

    def test_zero_vig_matches_raw(self):
        result = devig_shin([100, -100])
        self.assertAlmostEqual(result.fair_probabilities[0], 0.5, places=6)
        self.assertAlmostEqual(result.fair_probabilities[1], 0.5, places=6)


class TestPower(unittest.TestCase):
    def test_sums_to_one(self):
        for odds in ([150, -170], [-110, -110], [500, 250, 2000], [-200, 150, 800]):
            result = devig_power(odds)
            self.assertAlmostEqual(sum(result.fair_probabilities), 1.0, places=6, msg=odds)
            for p in result.fair_probabilities:
                self.assertGreater(p, 0.0, msg=odds)
                self.assertLess(p, 1.0, msg=odds)

    def test_root_actually_solves_defining_equation(self):
        odds = [500, 250, 2000]
        raw = [american_to_prob(o) for o in odds]
        result = devig_power(odds)
        # Recover k from fair_0 = raw_0 ^ k, then confirm it reproduces
        # fair_1 and fair_2 too (i.e. one shared k explains all outcomes).
        k_recovered = math.log(result.fair_probabilities[0]) / math.log(raw[0])
        for r, f in zip(raw[1:], result.fair_probabilities[1:]):
            self.assertAlmostEqual(r ** k_recovered, f, places=4)

    def test_zero_vig_matches_raw(self):
        result = devig_power([100, -100])
        self.assertAlmostEqual(result.fair_probabilities[0], 0.5, places=6)
        self.assertAlmostEqual(result.fair_probabilities[1], 0.5, places=6)


class TestRealisticThreeWayMarket(unittest.TestCase):
    """The three-way fixtures elsewhere in this file (+500/+250/+2000,
    taken directly from the user's own spec example) sum to a raw
    probability of 0.5 - a "negative vig" input no real book would post,
    since it's pure arbitrage. Useful for exercising the algorithms'
    sum<1 handling, but not representative of what this tool will
    actually see. This class uses a realistic vigged 3-way market
    (-150/+150/+400, raw probabilities summing to 1.2 - a 20% hold) so
    every method is also verified against genuine market-shaped input,
    not just the edge case."""

    ODDS = [-150, 150, 400]  # raw probs: 0.6, 0.4, 0.2 -> sum 1.2

    def test_all_methods_sum_to_one_and_valid(self):
        for name, fn in [("multiplicative", devig_multiplicative),
                          ("additive_linear", devig_additive_linear),
                          ("shin", devig_shin),
                          ("power", devig_power),
                          ("worst_case", devig_worst_case)]:
            result = fn(self.ODDS)
            self.assertAlmostEqual(sum(result.fair_probabilities), 1.0, places=6, msg=name)
            for p in result.fair_probabilities:
                self.assertGreater(p, 0.0, msg=name)
                self.assertLess(p, 1.0, msg=name)

    def test_multiplicative_hand_computed(self):
        raw = [0.6, 0.4, 0.2]
        total = 1.2
        expected = [r / total for r in raw]
        result = devig_multiplicative(self.ODDS)
        for got, want in zip(result.fair_probabilities, expected):
            self.assertAlmostEqual(got, want, places=8)

    def test_worst_case_only_favorite_untouched_leg_moves(self):
        # Longest leg is +400 (raw 0.2, the smallest).
        raw = [0.6, 0.4, 0.2]
        excess = sum(raw) - 1.0  # 0.2
        result = devig_worst_case(self.ODDS)
        self.assertAlmostEqual(result.fair_probabilities[2], 0.2 - excess, places=8)
        self.assertAlmostEqual(result.fair_probabilities[0], 0.6, places=8)
        self.assertAlmostEqual(result.fair_probabilities[1], 0.4, places=8)


class TestDispatcher(unittest.TestCase):
    def test_unknown_method_rejected(self):
        with self.assertRaises(ValueError):
            devig([100, -100], method="not_a_real_method")

    def test_single_outcome_rejected(self):
        with self.assertRaises(ValueError):
            devig([100], method="multiplicative")

    def test_default_method_is_multiplicative(self):
        result = devig([150, -170])
        expected = devig_multiplicative([150, -170])
        self.assertEqual(result.fair_probabilities, expected.fair_probabilities)

    def test_all_method_names_resolve(self):
        for name in ["multiplicative", "additive", "additive_linear", "shin", "power", "worst_case"]:
            result = devig([150, -170], method=name)
            self.assertAlmostEqual(sum(result.fair_probabilities), 1.0, places=6, msg=name)


class TestFourMethodsDivergeOnAsymmetricMarket(unittest.TestCase):
    """Sanity check that the four methods are actually doing different
    things on a real asymmetric market (not, say, accidentally all
    calling the same code path) - and that all four still produce valid
    probability distributions."""

    def test_all_four_valid_and_not_identical(self):
        odds = [500, -700]
        results = {
            "multiplicative": devig_multiplicative(odds),
            "additive_linear": devig_additive_linear(odds),
            "shin": devig_shin(odds),
            "power": devig_power(odds),
            "worst_case": devig_worst_case(odds),
        }
        for name, r in results.items():
            self.assertAlmostEqual(sum(r.fair_probabilities), 1.0, places=6, msg=name)
            for p in r.fair_probabilities:
                self.assertGreater(p, 0.0, msg=name)
                self.assertLess(p, 1.0, msg=name)

        favorite_probs = {name: r.fair_probabilities[1] for name, r in results.items()}
        distinct_values = set(round(v, 6) for v in favorite_probs.values())
        self.assertGreater(len(distinct_values), 1,
                            f"All methods produced the same favorite probability: {favorite_probs}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
