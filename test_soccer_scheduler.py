"""Unit tests for soccer_scheduler.py (2026-09-16) - the kickoff-aware
intraday scheduler built after confirming live that soccer_dns_daily_
digest.yml's once-daily cron missed its narrow due-window on a GitHub
scheduling delay and produced a full day with zero real runs.

Run with: python -m unittest test_soccer_scheduler -v
"""

import unittest
from datetime import datetime, timedelta, timezone

import soccer_scheduler as sched

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestIntervalMinutesFor(unittest.TestCase):
    """Window-boundary tests - the exact thing the user asked for."""

    def test_just_over_24h_uses_far_interval(self):
        self.assertEqual(sched.interval_minutes_for(24.01), sched.FAR_INTERVAL_MINUTES)

    def test_exactly_24h_uses_mid_interval(self):
        # >24h is FAR; exactly 24h falls into the 24h-3h (mid) band.
        self.assertEqual(sched.interval_minutes_for(24.0), sched.MID_INTERVAL_MINUTES)

    def test_just_under_24h_uses_mid_interval(self):
        self.assertEqual(sched.interval_minutes_for(23.99), sched.MID_INTERVAL_MINUTES)

    def test_just_over_3h_uses_mid_interval(self):
        self.assertEqual(sched.interval_minutes_for(3.01), sched.MID_INTERVAL_MINUTES)

    def test_exactly_3h_uses_near_interval(self):
        self.assertEqual(sched.interval_minutes_for(3.0), sched.NEAR_INTERVAL_MINUTES)

    def test_just_under_3h_uses_near_interval(self):
        self.assertEqual(sched.interval_minutes_for(2.99), sched.NEAR_INTERVAL_MINUTES)

    def test_just_before_kickoff_uses_near_interval(self):
        self.assertEqual(sched.interval_minutes_for(0.01), sched.NEAR_INTERVAL_MINUTES)

    def test_at_kickoff_is_none(self):
        self.assertIsNone(sched.interval_minutes_for(0))

    def test_after_kickoff_is_none(self):
        self.assertIsNone(sched.interval_minutes_for(-1))

    def test_unknown_hours_is_none(self):
        self.assertIsNone(sched.interval_minutes_for(None))


class TestAnyFixtureDue(unittest.TestCase):
    def test_never_checked_is_due_for_any_upcoming_fixture(self):
        fixtures = [{"kickoff": _iso(NOW + timedelta(hours=5))}]
        self.assertTrue(sched.any_fixture_due(fixtures, None, now=NOW))

    def test_no_upcoming_fixtures_is_not_due(self):
        fixtures = [{"kickoff": _iso(NOW - timedelta(hours=1))}]
        self.assertFalse(sched.any_fixture_due(fixtures, None, now=NOW))

    def test_within_far_band_interval_is_not_due(self):
        fixtures = [{"kickoff": _iso(NOW + timedelta(hours=30))}]  # >24h -> far band
        last_checked = _iso(NOW - timedelta(minutes=sched.FAR_INTERVAL_MINUTES - 10))
        self.assertFalse(sched.any_fixture_due(fixtures, last_checked, now=NOW))

    def test_past_far_band_interval_is_due(self):
        fixtures = [{"kickoff": _iso(NOW + timedelta(hours=30))}]
        last_checked = _iso(NOW - timedelta(minutes=sched.FAR_INTERVAL_MINUTES + 10))
        self.assertTrue(sched.any_fixture_due(fixtures, last_checked, now=NOW))

    def test_past_mid_band_interval_is_due(self):
        fixtures = [{"kickoff": _iso(NOW + timedelta(hours=10))}]  # 24h-3h -> mid band
        last_checked = _iso(NOW - timedelta(minutes=sched.MID_INTERVAL_MINUTES + 5))
        self.assertTrue(sched.any_fixture_due(fixtures, last_checked, now=NOW))

    def test_past_near_band_interval_is_due(self):
        fixtures = [{"kickoff": _iso(NOW + timedelta(hours=1))}]  # final 3h -> near band
        last_checked = _iso(NOW - timedelta(minutes=sched.NEAR_INTERVAL_MINUTES + 2))
        self.assertTrue(sched.any_fixture_due(fixtures, last_checked, now=NOW))

    def test_a_fixture_crossing_into_a_tighter_band_becomes_due_again(self):
        """The exact scenario the tiered design exists for: checked
        recently under the loose far-band interval, but the fixture has
        since crossed into the tighter mid band."""
        fixtures = [{"kickoff": _iso(NOW + timedelta(hours=23, minutes=59))}]  # now in mid band
        last_checked = _iso(NOW - timedelta(minutes=sched.MID_INTERVAL_MINUTES + 1))
        self.assertTrue(sched.any_fixture_due(fixtures, last_checked, now=NOW))

    def test_multiple_fixtures_any_one_due_is_enough(self):
        fixtures = [
            {"kickoff": _iso(NOW + timedelta(hours=30))},  # far band, recently checked
            {"kickoff": _iso(NOW + timedelta(hours=1))},   # near band, overdue
        ]
        last_checked = _iso(NOW - timedelta(minutes=sched.NEAR_INTERVAL_MINUTES + 5))
        self.assertTrue(sched.any_fixture_due(fixtures, last_checked, now=NOW))

    def test_fixture_with_unparseable_kickoff_is_skipped_not_a_crash(self):
        fixtures = [{"kickoff": "not-a-date"}]
        self.assertFalse(sched.any_fixture_due(fixtures, None, now=NOW))

    def test_empty_fixture_list_is_not_due(self):
        self.assertFalse(sched.any_fixture_due([], None, now=NOW))


class TestBoardFetchDue(unittest.TestCase):
    def test_never_fetched_is_due(self):
        due, reason = sched.board_fetch_due(None, [], set(), now=NOW)
        self.assertTrue(due)
        self.assertEqual(reason, "never_fetched")

    def test_within_min_interval_and_no_kickoff_proximity_is_not_due(self):
        last_fetch = _iso(NOW - timedelta(minutes=sched.BOARD_MIN_INTERVAL_MINUTES - 5))
        fixtures = [{"fixture_id": "f1", "kickoff": _iso(NOW + timedelta(hours=10))}]
        due, reason = sched.board_fetch_due(last_fetch, fixtures, set(), now=NOW)
        self.assertFalse(due)

    def test_past_min_interval_is_due(self):
        last_fetch = _iso(NOW - timedelta(minutes=sched.BOARD_MIN_INTERVAL_MINUTES + 5))
        due, reason = sched.board_fetch_due(last_fetch, [], set(), now=NOW)
        self.assertTrue(due)
        self.assertEqual(reason, "interval_elapsed")

    def test_kickoff_within_proximity_window_forces_a_fetch(self):
        last_fetch = _iso(NOW - timedelta(minutes=5))  # well within the min interval
        fixtures = [{"fixture_id": "f1", "kickoff": _iso(NOW + timedelta(minutes=60))}]
        due, reason = sched.board_fetch_due(last_fetch, fixtures, set(), now=NOW)
        self.assertTrue(due)
        self.assertEqual(reason, "kickoff_proximity:f1")

    def test_kickoff_proximity_already_done_for_that_fixture_does_not_force_again(self):
        last_fetch = _iso(NOW - timedelta(minutes=5))
        fixtures = [{"fixture_id": "f1", "kickoff": _iso(NOW + timedelta(minutes=60))}]
        due, reason = sched.board_fetch_due(last_fetch, fixtures, {"f1"}, now=NOW)
        self.assertFalse(due)

    def test_kickoff_outside_proximity_window_does_not_force(self):
        last_fetch = _iso(NOW - timedelta(minutes=5))
        fixtures = [{"fixture_id": "f1", "kickoff": _iso(NOW + timedelta(minutes=200))}]
        due, reason = sched.board_fetch_due(last_fetch, fixtures, set(), now=NOW)
        self.assertFalse(due)

    def test_kickoff_already_passed_does_not_force(self):
        last_fetch = _iso(NOW - timedelta(minutes=5))
        fixtures = [{"fixture_id": "f1", "kickoff": _iso(NOW - timedelta(minutes=10))}]
        due, reason = sched.board_fetch_due(last_fetch, fixtures, set(), now=NOW)
        self.assertFalse(due)


class TestStatePersistence(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        import unittest.mock
        self._patch = unittest.mock.patch.object(sched, "STATE_PATH",
                                                   __import__("os").path.join(self._tmp.name, "state.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_load_state_with_no_file_returns_defaults(self):
        state = sched.load_state()
        self.assertIsNone(state["last_checked_at"])
        self.assertIsNone(state["last_board_fetch_at"])
        self.assertEqual(state["kickoff_proximity_done"], [])

    def test_save_then_load_roundtrips(self):
        sched.save_state({"last_checked_at": "2026-09-16T12:00:00Z",
                           "last_board_fetch_at": "2026-09-16T11:30:00Z",
                           "kickoff_proximity_done": ["f1", "f2"]})
        state = sched.load_state()
        self.assertEqual(state["last_checked_at"], "2026-09-16T12:00:00Z")
        self.assertEqual(state["kickoff_proximity_done"], ["f1", "f2"])

    def test_malformed_state_file_falls_back_to_defaults(self):
        import os
        os.makedirs(os.path.dirname(sched.STATE_PATH), exist_ok=True)
        with open(sched.STATE_PATH, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        state = sched.load_state()
        self.assertIsNone(state["last_checked_at"])


if __name__ == "__main__":
    unittest.main()
