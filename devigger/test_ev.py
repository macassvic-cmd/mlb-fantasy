"""Unit tests for ev.py. Run with: python -m unittest test_ev -v"""

import unittest

from ev import pickem_ev, kelly_sizing


class TestPickemEV(unittest.TestCase):
    def test_exact_breakeven(self):
        result = pickem_ev(0.5, 2.0)
        self.assertAlmostEqual(result.ev_fraction, 0.0, places=8)
        self.assertAlmostEqual(result.breakeven_probability, 0.5, places=8)
        self.assertAlmostEqual(result.edge_probability_points, 0.0, places=8)
        self.assertFalse(result.clears_breakeven)  # exactly 0 does not "clear"

    def test_positive_ev(self):
        result = pickem_ev(0.6, 2.0)
        self.assertAlmostEqual(result.ev_fraction, 0.2, places=8)
        self.assertAlmostEqual(result.edge_probability_points, 0.1, places=8)
        self.assertTrue(result.clears_breakeven)

    def test_negative_ev(self):
        result = pickem_ev(0.4, 2.0)
        self.assertAlmostEqual(result.ev_fraction, -0.2, places=8)
        self.assertFalse(result.clears_breakeven)

    def test_three_leg_pickem_example(self):
        # A common pick'em structure: 3 legs pays 5x total. Fair prob 0.25
        # (e.g. three roughly-coinflip legs correlated down) vs breakeven 0.20.
        result = pickem_ev(0.25, 5.0)
        self.assertAlmostEqual(result.breakeven_probability, 0.2, places=8)
        self.assertAlmostEqual(result.ev_fraction, 0.25, places=8)  # 0.25*5-1 = 0.25
        self.assertTrue(result.clears_breakeven)

    def test_invalid_probability_rejected(self):
        with self.assertRaises(ValueError):
            pickem_ev(0.0, 2.0)
        with self.assertRaises(ValueError):
            pickem_ev(1.0, 2.0)
        with self.assertRaises(ValueError):
            pickem_ev(1.5, 2.0)

    def test_invalid_multiplier_rejected(self):
        with self.assertRaises(ValueError):
            pickem_ev(0.5, 1.0)
        with self.assertRaises(ValueError):
            pickem_ev(0.5, 0.5)


class TestKellySizing(unittest.TestCase):
    def test_matches_cnm_reference(self):
        # Real CNM reference: correlated fair prob ~5.6%, offered
        # +2800 (29.0x total return) -> "Kelly $5.60 (Full 2.24u)".
        # Using OUR validated correlated probability (5.6275%, see
        # test_correlation.py) rather than CNM's rounded 5.6% display.
        result = kelly_sizing(0.056275, 29.0)
        self.assertAlmostEqual(result.full_kelly_fraction * 100, 2.24, delta=0.05)

    def test_half_and_quarter_are_exact_fractions(self):
        result = kelly_sizing(0.10, 5.0)
        self.assertAlmostEqual(result.half_kelly_fraction, result.full_kelly_fraction / 2, places=10)
        self.assertAlmostEqual(result.quarter_kelly_fraction, result.full_kelly_fraction / 4, places=10)

    def test_negative_kelly_clamps_to_zero(self):
        # A -EV bet (fair prob well below breakeven) should never
        # recommend a negative stake.
        result = kelly_sizing(0.10, 2.0)  # breakeven is 50%, this is -EV
        self.assertEqual(result.full_kelly_fraction, 0.0)
        self.assertEqual(result.half_kelly_fraction, 0.0)

    def test_dollar_amounts_only_when_bankroll_given(self):
        no_bankroll = kelly_sizing(0.06, 29.0)
        self.assertIsNone(no_bankroll.full_kelly_dollars)

        with_bankroll = kelly_sizing(0.06, 29.0, bankroll=1000.0)
        self.assertIsNotNone(with_bankroll.full_kelly_dollars)
        self.assertAlmostEqual(with_bankroll.full_kelly_dollars,
                                with_bankroll.full_kelly_fraction * 1000.0, places=8)

    def test_invalid_inputs_rejected(self):
        with self.assertRaises(ValueError):
            kelly_sizing(0.0, 2.0)
        with self.assertRaises(ValueError):
            kelly_sizing(0.5, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
