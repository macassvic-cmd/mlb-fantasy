"""Unit tests for soccer_dates.py - the shared Pacific-anchored "what day
is it" helper used across the Soccer DNS stack (2026-09-14 fix for the
exact bug found live: the daily digest stamped a snapshot "2026-09-15"
at 22:53 PT / 05:53 UTC the next day, purely from a bare UTC date calc).

Run with: python -m unittest test_soccer_dates -v
"""

import unittest
from datetime import datetime, timezone

from soccer_dates import soccer_today_str, format_kickoff_pacific


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


class TestFormatKickoffPacific(unittest.TestCase):
    """2026-09-16 Hinshelwood item 4 - alerts (Discord + dashboard) were
    showing raw UTC ISO strings ('2026-09-16T19:00:00.000Z') as-is."""

    def test_converts_utc_iso_to_readable_pacific(self):
        # 2026-09-16T19:00:00Z is a September date -> PDT (UTC-7).
        result = format_kickoff_pacific("2026-09-16T19:00:00.000Z")
        self.assertEqual(result, "Wed 9/16, 12:00 PM PDT")

    def test_midnight_utc_rolls_to_previous_pacific_day(self):
        result = format_kickoff_pacific("2026-09-17T02:00:00.000Z")
        self.assertEqual(result, "Wed 9/16, 7:00 PM PDT")

    def test_none_passes_through_unchanged(self):
        self.assertIsNone(format_kickoff_pacific(None))

    def test_unparseable_value_falls_back_to_original_string(self):
        self.assertEqual(format_kickoff_pacific("time TBD"), "time TBD")


if __name__ == "__main__":
    unittest.main()
