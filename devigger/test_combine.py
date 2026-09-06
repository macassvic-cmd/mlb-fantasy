"""Unit tests for combine.py. Run with: python -m unittest test_combine -v"""

import unittest

from combine import evaluate
from devig import american_to_prob, devig_multiplicative


class TestSingleLeg(unittest.TestCase):
    def test_no_vig_two_way(self):
        result = evaluate("+100/-100", method="multiplicative")
        self.assertAlmostEqual(result.overall_fair_probability, 0.5, places=6)

    def test_hand_computed_asymmetric(self):
        result = evaluate("+150/-170", method="multiplicative")
        expected = devig_multiplicative([150, -170]).fair_probabilities[0]
        self.assertAlmostEqual(result.overall_fair_probability, expected, places=8)


class TestParlayCombination(unittest.TestCase):
    def test_two_leg_parlay_multiplies_fair_probabilities(self):
        result = evaluate("+150/-170,-115/-105", method="multiplicative")
        leg1_fair = devig_multiplicative([150, -170]).fair_probabilities[0]
        leg2_fair = devig_multiplicative([-115, -105]).fair_probabilities[0]
        expected = leg1_fair * leg2_fair
        self.assertAlmostEqual(result.overall_fair_probability, expected, places=8)
        self.assertEqual(len(result.group_evaluations), 1)
        self.assertEqual(len(result.group_evaluations[0].leg_evaluations), 2)

    def test_three_leg_parlay(self):
        result = evaluate("+100,+100,+100", method="multiplicative")
        # each leg is fair-value-shorthand -> exactly 0.5 fair prob
        self.assertAlmostEqual(result.overall_fair_probability, 0.125, places=8)


class TestOrCombination(unittest.TestCase):
    def test_two_independent_groups_union_formula(self):
        result = evaluate("-115/-110||-185/+140", method="multiplicative")
        p1 = devig_multiplicative([-115, -110]).fair_probabilities[0]
        p2 = devig_multiplicative([-185, 140]).fair_probabilities[0]
        expected = 1.0 - (1.0 - p1) * (1.0 - p2)
        self.assertAlmostEqual(result.overall_fair_probability, expected, places=8)
        self.assertEqual(len(result.group_evaluations), 2)

    def test_or_probability_exceeds_either_individual_group(self):
        result = evaluate("-115/-110||-185/+140", method="multiplicative")
        p1 = result.group_evaluations[0].combined_fair_probability
        p2 = result.group_evaluations[1].combined_fair_probability
        self.assertGreater(result.overall_fair_probability, p1)
        self.assertGreater(result.overall_fair_probability, p2)

    def test_or_group_can_itself_be_a_parlay(self):
        result = evaluate("+100,+100||+150", method="multiplicative")
        parlay_p = 0.5 * 0.5
        single_p = devig_multiplicative([150, -150]).fair_probabilities[0]
        expected = 1.0 - (1.0 - parlay_p) * (1.0 - single_p)
        self.assertAlmostEqual(result.overall_fair_probability, expected, places=8)


class TestXorMultiwayFeedsDevigCorrectly(unittest.TestCase):
    def test_xor_market_devigs_like_native_multiway(self):
        # +500/^+800/^+1000 should devig identically to a native
        # +500/+800/+1000 multi-way, since the math doesn't distinguish
        # provenance - only combined_from_separate_markets differs.
        xor_result = evaluate("+500/^+800/^+1000", method="multiplicative")
        native_result = evaluate("+500/+800/+1000", method="multiplicative")
        self.assertAlmostEqual(xor_result.overall_fair_probability,
                                native_result.overall_fair_probability, places=8)
        leg = xor_result.group_evaluations[0].leg_evaluations[0]
        self.assertTrue(leg.leg.market.combined_from_separate_markets)


class TestMethodSelection(unittest.TestCase):
    def test_method_flows_through_to_devig_result(self):
        for method in ["multiplicative", "additive_linear", "shin", "power", "worst_case"]:
            result = evaluate("+150/-170", method=method)
            self.assertEqual(result.method, method)
            self.assertEqual(result.group_evaluations[0].leg_evaluations[0].devig_result.method,
                              method if method != "additive_linear" else "additive_linear")


if __name__ == "__main__":
    unittest.main(verbosity=2)
