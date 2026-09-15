"""Unit tests for soccer_dates.py - the shared Pacific-anchored "what day
is it" helper used across the Soccer DNS stack (2026-09-14 fix for the
exact bug found live: the daily digest stamped a snapshot "2026-09-15"
at 22:53 PT / 05:53 UTC the next day, purely from a bare UTC date calc).

Run with: python -m unittest test_soccer_dates -v
"""

import unittest
from datetime import datetime, timezone

from soccer_dates import soccer_today_str


class TestSoccerTodayStr(unittest.TestCase):
    def test_late_evening_pacific_does_not_roll_to_next_day(self):
        # 2026-09-15 05:53 UTC = 2026-09-14 22:53 PDT - the exact live
        # case that motivated this fix.
        now = datetime(2026, 9, 15, 5, 53, tzinfo=timezone.utc)
        self.assertEqual(soccer_today_str(now), "2026-09-14")

    def test_pacific_morning_matches_utc_same_day(self):
        now = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(soccer_today_str(now), "2026-09-14")

    def test_naive_datetime_assumed_utc(self):
        now = datetime(2026, 9, 15, 5, 53)
        self.assertEqual(soccer_today_str(now), "2026-09-14")


if __name__ == "__main__":
    unittest.main()
