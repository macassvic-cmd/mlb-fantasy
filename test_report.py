"""Unit tests for report.py's lineup_freshness_signals (2026-09-15 item
6) - the merged dashboard freshness status needs to know whether any of
today's games hasn't started with an unconfirmed lineup, the same
question check_pipeline_freshness.py's _any_unconfirmed_lineup_pending
answers for the CI gate.

Run with: python -m unittest test_report -v
"""

import unittest
from datetime import datetime, timedelta, timezone

from report import lineup_freshness_signals

NOW = datetime(2026, 9, 15, 22, 0, 0, tzinfo=timezone.utc)


def _row(game_offset_hours, lineup_confirmed):
    game_time = NOW + timedelta(hours=game_offset_hours)
    return {"game_date_utc": game_time.strftime("%Y-%m-%dT%H:%M:%SZ"), "lineup_confirmed": lineup_confirmed}


class TestLineupFreshnessSignals(unittest.TestCase):
    def test_empty_rows_all_started_true_nothing_pending(self):
        all_started, pending = lineup_freshness_signals([], now=NOW)
        self.assertTrue(all_started)
        self.assertFalse(pending)

    def test_all_games_started_when_every_game_time_is_in_the_past(self):
        rows = [_row(-1, True), _row(-2, False)]
        all_started, pending = lineup_freshness_signals(rows, now=NOW)
        self.assertTrue(all_started)
        self.assertFalse(pending, "an unconfirmed lineup for an already-started game is no longer pending")

    def test_unconfirmed_lineup_for_upcoming_game_is_pending(self):
        rows = [_row(2, False)]
        all_started, pending = lineup_freshness_signals(rows, now=NOW)
        self.assertFalse(all_started)
        self.assertTrue(pending)

    def test_confirmed_lineup_for_upcoming_game_is_not_pending(self):
        rows = [_row(2, True)]
        all_started, pending = lineup_freshness_signals(rows, now=NOW)
        self.assertFalse(all_started)
        self.assertFalse(pending)

    def test_mixed_started_and_upcoming_games(self):
        rows = [_row(-1, False), _row(3, False)]
        all_started, pending = lineup_freshness_signals(rows, now=NOW)
        self.assertFalse(all_started)
        self.assertTrue(pending)

    def test_missing_game_date_is_skipped_not_a_crash(self):
        rows = [{"lineup_confirmed": False}]
        all_started, pending = lineup_freshness_signals(rows, now=NOW)
        self.assertTrue(all_started)
        self.assertFalse(pending)

    def test_malformed_game_date_is_skipped_not_a_crash(self):
        rows = [{"game_date_utc": "not-a-date", "lineup_confirmed": False}]
        all_started, pending = lineup_freshness_signals(rows, now=NOW)
        self.assertTrue(all_started)
        self.assertFalse(pending)

    def test_lineup_confirmed_defaults_to_false_when_field_missing(self):
        rows = [{"game_date_utc": (NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")}]
        all_started, pending = lineup_freshness_signals(rows, now=NOW)
        self.assertTrue(pending)


if __name__ == "__main__":
    unittest.main()
