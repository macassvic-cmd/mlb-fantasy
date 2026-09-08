"""Unit tests for correlation.py. Run with: python -m unittest test_correlation -v"""

import unittest

from correlation import correlated_group_probability, evaluate_correlated, parse_sgp_specs
from combine import evaluate


class TestParseSgpSpecs(unittest.TestCase):
    def test_parses_reference_example(self):
        specs = parse_sgp_specs("3:0.33,2:0.14")
        self.assertEqual(specs, [(3, 0.33), (2, 0.14)])

    def test_single_group(self):
        self.assertEqual(parse_sgp_specs("5:0.5"), [(5, 0.5)])

    def test_malformed_rejected(self):
        with self.assertRaises(ValueError):
            parse_sgp_specs("not_a_spec")
        with self.assertRaises(ValueError):
            parse_sgp_specs("")
        with self.assertRaises(ValueError):
            parse_sgp_specs("0:0.5")


class TestCorrelatedGroupProbability(unittest.TestCase):
    def test_r_zero_equals_independent_product(self):
        probs = [0.5, 0.6, 0.7]
        independent = 0.5 * 0.6 * 0.7
        self.assertAlmostEqual(correlated_group_probability(probs, 0.0), independent, places=8)

    def test_r_one_equals_min_leg(self):
        probs = [0.5, 0.6, 0.7]
        self.assertAlmostEqual(correlated_group_probability(probs, 1.0), min(probs), places=8)

    def test_r_half_is_midpoint(self):
        probs = [0.4, 0.5]
        independent = 0.4 * 0.5
        p_min = 0.4
        expected = independent + 0.5 * (p_min - independent)
        self.assertAlmostEqual(correlated_group_probability(probs, 0.5), expected, places=8)

    def test_correlated_always_at_least_independent(self):
        # Correlation only ever INCREASES the group probability relative
        # to naive independence (or leaves it unchanged at r=0) - a
        # positively-correlated same-game parlay is never less likely
        # than treating its legs as independent.
        probs = [0.3, 0.55, 0.8]
        independent = 0.3 * 0.55 * 0.8
        for r in [0.0, 0.1, 0.33, 0.5, 0.75, 1.0]:
            self.assertGreaterEqual(correlated_group_probability(probs, r), independent - 1e-12)

    def test_invalid_r_rejected(self):
        with self.assertRaises(ValueError):
            correlated_group_probability([0.5, 0.5], -0.1)
        with self.assertRaises(ValueError):
            correlated_group_probability([0.5, 0.5], 1.1)

    def test_single_leg_group(self):
        # A "group" of one leg has no correlation to blend toward -
        # independent product IS that one probability, and min IS that
        # one probability too, so any r gives the same answer.
        for r in [0.0, 0.5, 1.0]:
            self.assertAlmostEqual(correlated_group_probability([0.42], r), 0.42, places=8)


class TestEvaluateCorrelatedAgainstCnmReference(unittest.TestCase):
    """The actual validation: CNM's real reference output for a 5-leg
    parlay (3-leg SGP at r=0.33, 2-leg SGP at r=0.14) - see
    correlation.py's module docstring."""

    ODDS = "+100%,-120%,-165%,+100%,-110%"
    SGP_SPECS = [(3, 0.33), (2, 0.14)]

    def test_matches_cnm_uncorrelated(self):
        result = evaluate_correlated(self.ODDS, self.SGP_SPECS)
        # CNM reported 2.6% / +3675
        self.assertAlmostEqual(result.uncorrelated_probability * 100, 2.6, delta=0.05)

    def test_matches_cnm_correlated(self):
        result = evaluate_correlated(self.ODDS, self.SGP_SPECS)
        # CNM reported 5.6% / +1683
        self.assertAlmostEqual(result.correlated_probability * 100, 5.6, delta=0.05)

    def test_correlated_exceeds_uncorrelated(self):
        result = evaluate_correlated(self.ODDS, self.SGP_SPECS)
        self.assertGreater(result.correlated_probability, result.uncorrelated_probability)

    def test_group_breakdown_matches_leg_count(self):
        result = evaluate_correlated(self.ODDS, self.SGP_SPECS)
        self.assertEqual(len(result.groups), 2)
        self.assertEqual(result.groups[0].leg_count, 3)
        self.assertEqual(result.groups[0].r, 0.33)
        self.assertEqual(result.groups[1].leg_count, 2)
        self.assertEqual(result.groups[1].r, 0.14)

    def test_overall_matches_manual_recomputation_from_our_own_legs(self):
        # Cross-check evaluate_correlated's own product-of-groups logic
        # against independently recombining the same underlying leg
        # evaluations from combine.evaluate - these two code paths
        # should never disagree with each other regardless of whether
        # they match CNM.
        plain = evaluate(self.ODDS, method="multiplicative")
        uncorr_via_plain = 1.0
        for le in plain.group_evaluations[0].leg_evaluations:
            uncorr_via_plain *= le.fair_probability
        result = evaluate_correlated(self.ODDS, self.SGP_SPECS)
        self.assertAlmostEqual(result.uncorrelated_probability, uncorr_via_plain, places=8)


class TestEvaluateCorrelatedValidation(unittest.TestCase):
    def test_rejects_or_groups(self):
        with self.assertRaises(ValueError):
            evaluate_correlated("+100%||+150%", [(1, 0.0), (1, 0.0)])

    def test_rejects_mismatched_leg_count(self):
        with self.assertRaises(ValueError):
            evaluate_correlated("+100%,-120%,-165%", [(2, 0.33)])  # covers 2, input has 3


if __name__ == "__main__":
    unittest.main(verbosity=2)
