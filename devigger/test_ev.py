"""Unit tests for ev.py. Run with: python -m unittest test_ev -v"""

import unittest

from ev import pickem_ev


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
