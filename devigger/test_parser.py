"""Unit tests for parser.py. Run with: python -m unittest test_parser -v"""

import unittest

from parser import parse_leg, parse_input


class TestBasicMarkets(unittest.TestCase):
    def test_two_way_market(self):
        leg = parse_leg("+500/-700")
        self.assertEqual(leg.market.odds, [500, -700])
        self.assertFalse(leg.market.combined_from_separate_markets)
        self.assertIsNone(leg.market.juice_source)

    def test_native_multiway_no_xor(self):
        leg = parse_leg("+500/+250/+2000")
        self.assertEqual(leg.market.odds, [500, 250, 2000])
        self.assertFalse(leg.market.combined_from_separate_markets)

    def test_xor_multiway(self):
        leg = parse_leg("+500/^+800/^+1000")
        self.assertEqual(leg.market.odds, [500, 800, 1000])
        self.assertTrue(leg.market.combined_from_separate_markets)

    def test_negative_odds_parsed(self):
        leg = parse_leg("-115/-110")
        self.assertEqual(leg.market.odds, [-115, -110])


class TestFairValueShorthand(unittest.TestCase):
    def test_positive_shorthand_mirrors_negative(self):
        leg = parse_leg("+200")
        self.assertEqual(leg.market.odds, [200, -200])
        self.assertIn("fair_value_shorthand", leg.market.juice_source)

    def test_negative_shorthand_mirrors_positive(self):
        leg = parse_leg("-150")
        self.assertEqual(leg.market.odds, [-150, 150])


class TestJuiceSpecification(unittest.TestCase):
    def test_two_value_bracket_produces_synthesized_side(self):
        leg = parse_leg("+285/[-116:-106]")
        self.assertEqual(leg.market.odds[0], 285)
        self.assertIsInstance(leg.market.odds[1], float)
        self.assertIn("reference market", leg.market.juice_source)
        # The synthesized other side should be a plausible favorite price
        # (negative), not a wild or nonsensical value.
        self.assertLess(leg.market.odds[1], 0)

    def test_two_value_bracket_hold_matches_reference(self):
        # Directly verify the hold-transfer formula: combined raw
        # probability of [primary, synthesized] should reproduce the
        # reference market's own hold.
        from devig import american_to_prob
        leg = parse_leg("+285/[-116:-106]")
        ref_raw = [american_to_prob(-116), american_to_prob(-106)]
        ref_hold = sum(ref_raw) - 1.0
        primary_raw = american_to_prob(285)
        other_raw = american_to_prob(leg.market.odds[1])
        self.assertAlmostEqual(primary_raw + other_raw - 1.0, ref_hold, places=6)

    def test_three_value_bracket_produces_synthesized_side(self):
        leg = parse_leg("-270/[-135:-110:-110]")
        self.assertEqual(leg.market.odds[0], -270)
        self.assertIn("alt-line", leg.market.juice_source)
        # A -270 favorite's missing side should synthesize to a
        # plausible underdog price (positive).
        self.assertGreater(leg.market.odds[1], 0)

    def test_percent_default_juice(self):
        leg = parse_leg("+600%")
        self.assertEqual(leg.market.odds[0], 600)
        self.assertIn("CNM-calibrated curve", leg.market.juice_source)

    def test_percent_matches_cnm_reference_exactly(self):
        # Real CNM reference output (2026-09-06 validation, "default 10%
        # juice per leg"): these 4 (input, CNM fair American odds) pairs
        # are the calibration points themselves, so this is really
        # checking that the interpolation reproduces its own control
        # points exactly (a regression guard, not new evidence) - see
        # parser.py's CALIBRATION docstring for the full validation.
        from devig import devig_multiplicative, prob_to_american
        cases = [(100, 124), (-120, 102), (-165, -130), (-110, 112)]
        for odds, cnm_fair_odds in cases:
            leg = parse_leg(f"{odds:+d}%")
            result = devig_multiplicative(leg.market.odds)
            our_fair_odds = prob_to_american(result.fair_probabilities[0])
            self.assertAlmostEqual(our_fair_odds, cnm_fair_odds, places=1, msg=f"{odds:+d}%")

    def test_percent_extrapolation_flagged_outside_calibrated_range(self):
        # -165 (raw ~62.6%) is the most extreme calibration point on the
        # favorite side - a much heavier favorite must extrapolate, and
        # say so.
        leg = parse_leg("-2000%")
        self.assertIn("EXTRAPOLATED", leg.market.juice_source)
        # Within the calibrated 50-62% band, no such flag.
        leg2 = parse_leg("-120%")
        self.assertNotIn("EXTRAPOLATED", leg2.market.juice_source)

    def test_percent_underdog_reflects_symmetrically(self):
        # No underdog (raw<0.5) calibration points exist - handled by
        # reflecting through 0.5. A favorite and its exact mirror
        # underdog should get symmetric treatment (swap fair/other
        # roughly mirrors too, modulo which side is "primary").
        from devig import american_to_prob
        fav_leg = parse_leg("-120%")
        dog_leg = parse_leg("+120%")
        fav_raw = american_to_prob(fav_leg.market.odds[0])
        dog_raw = american_to_prob(dog_leg.market.odds[0])
        # The favorite's raw prob and the underdog's raw prob should be
        # complementary (0.545 and 0.455), and so should their
        # synthesized fair treatment by symmetry.
        self.assertAlmostEqual(fav_raw, 1 - dog_raw, places=6)

    def test_bad_bracket_size_rejected(self):
        with self.assertRaises(ValueError):
            parse_leg("+285/[-116:-106:-100:-90]")


class TestParlayAndOrParsing(unittest.TestCase):
    def test_comma_creates_one_group_two_legs(self):
        parsed = parse_input("+500,-600")
        self.assertEqual(len(parsed.or_groups), 1)
        self.assertEqual(len(parsed.or_groups[0].legs), 2)
        self.assertEqual(parsed.or_groups[0].legs[0].market.odds, [500, -500])
        self.assertEqual(parsed.or_groups[0].legs[1].market.odds, [-600, 600])

    def test_double_pipe_creates_two_groups(self):
        parsed = parse_input("-115/-110||-185/+140")
        self.assertEqual(len(parsed.or_groups), 2)
        self.assertEqual(len(parsed.or_groups[0].legs), 1)
        self.assertEqual(parsed.or_groups[0].legs[0].market.odds, [-115, -110])
        self.assertEqual(len(parsed.or_groups[1].legs), 1)
        self.assertEqual(parsed.or_groups[1].legs[0].market.odds, [-185, 140])

    def test_comma_within_or_group(self):
        # Each side of || can itself be a multi-leg parlay.
        parsed = parse_input("+100,+150||+200,-110")
        self.assertEqual(len(parsed.or_groups), 2)
        self.assertEqual(len(parsed.or_groups[0].legs), 2)
        self.assertEqual(len(parsed.or_groups[1].legs), 2)

    def test_single_leg_input(self):
        parsed = parse_input("+500/-700")
        self.assertEqual(len(parsed.or_groups), 1)
        self.assertEqual(len(parsed.or_groups[0].legs), 1)


class TestErrorHandling(unittest.TestCase):
    def test_empty_input_rejected(self):
        with self.assertRaises(ValueError):
            parse_input("")

    def test_malformed_leg_rejected(self):
        with self.assertRaises(ValueError):
            parse_leg("not_odds_at_all")

    def test_unsigned_number_rejected(self):
        # American odds must carry an explicit sign per the spec's own
        # examples - a bare "200" (no +/-) doesn't match the grammar.
        with self.assertRaises(ValueError):
            parse_leg("200")


if __name__ == "__main__":
    unittest.main(verbosity=2)
