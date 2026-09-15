"""Unit tests for soccer_alerts.py - focused on the 2026-09-14 fixes:
item 2 (a real-time alert must never fire at or after kickoff - found
live that Carles Gil kept generating fresh threshold-crossing alerts for
~12 hours after his actual 2026-09-13 21:30 UTC kickoff because Dabble's
board left the prop marked live and nothing here ever checked kickoff
time at all) and item 3 (one Discord message per scoring pass, the
HIGHEST threshold crossed - not one per threshold crossed in the same
pass).

No real network - send_discord_alert / requests.post is always mocked.
ALERTS_DIR is always patched to a tempfile.TemporaryDirectory() so a
test run can never touch real production alert data.

Run with: python -m unittest test_soccer_alerts -v
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import soccer_alerts as sa

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


def _candidate(dns_score=80, confidence_score=70, event_date=None, official_status="not_yet_posted",
               dabble_live=True, player_id="p1", fixture_id="f1", **overrides):
    c = {
        "dabble_player_id": player_id, "player_name": "Test Player", "team": "TST",
        "matchup": "TST @ OPP", "fixture_id": fixture_id, "event_date": event_date,
        "dns_score": dns_score, "confidence_score": confidence_score, "urgency_score": 0,
        "official_status": official_status, "dabble_status_raw": None, "dabble_live": dabble_live,
        "props": [], "top_reasons": [], "rotowire_status_raw": None, "prediction_consensus": "unknown",
        "x_signal": None,
    }
    c.update(overrides)
    return c


class TestKickoffCheck(unittest.TestCase):
    """item 2, 2026-09-14."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch_dir = patch("soccer_alerts.ALERTS_DIR", self._tmp.name)
        self._patch_dir.start()
        self._patch_send = patch("soccer_alerts.send_discord_alert", return_value=True)
        self.mock_send = self._patch_send.start()

    def tearDown(self):
        self._patch_send.stop()
        self._patch_dir.stop()
        self._tmp.cleanup()

    def test_alert_fires_normally_before_kickoff(self):
        future = (NOW + timedelta(hours=3)).isoformat()
        c = _candidate(dns_score=80, event_date=future)
        with patch("soccer_alerts.datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            data = sa.notify_soccer_candidates([c], date_str="2026-09-14")
        self.assertTrue(self.mock_send.called, "an upcoming fixture crossing a send threshold must alert")
        crossed_keys = [k for k in data if ":75" in k or ":80" in k]
        self.assertTrue(crossed_keys, "threshold crossings must still be recorded pre-kickoff")

    def test_alert_suppressed_after_kickoff_even_on_new_threshold_crossing(self):
        past = (NOW - timedelta(hours=12)).isoformat()
        c = _candidate(dns_score=97, event_date=past, dabble_live=True)
        with patch("soccer_alerts.datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            data = sa.notify_soccer_candidates([c], date_str="2026-09-14")
        self.assertFalse(self.mock_send.called, "must never send a real-time alert at or after kickoff")
        alert_keys = [k for k in data if k.startswith("2026-09-14:soccer:")]
        self.assertEqual(alert_keys, [], "no threshold/critical alert record should be created post-kickoff")

    def test_post_kickoff_still_live_recorded_as_data_problem(self):
        past = (NOW - timedelta(hours=12)).isoformat()
        c = _candidate(dns_score=97, event_date=past, dabble_live=True, player_id="p1", fixture_id="f1")
        with patch("soccer_alerts.datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            data = sa.notify_soccer_candidates([c], date_str="2026-09-14")
        stale_keys = [k for k in data if k.startswith(sa.STALE_KICKOFF_KEY_PREFIX)]
        self.assertEqual(len(stale_keys), 1)
        self.assertEqual(data[stale_keys[0]]["reason"], "still_live_past_kickoff")

    def test_unparseable_event_date_treated_as_data_problem_not_alert(self):
        c = _candidate(dns_score=97, event_date="not-a-real-date")
        with patch("soccer_alerts.datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            data = sa.notify_soccer_candidates([c], date_str="2026-09-14")
        self.assertFalse(self.mock_send.called, "unparseable kickoff time must never be alerted on blind")
        stale_keys = [k for k in data if k.startswith(sa.STALE_KICKOFF_KEY_PREFIX)]
        self.assertEqual(len(stale_keys), 1)
        self.assertEqual(data[stale_keys[0]]["reason"], "event_date_unparseable")

    def test_missing_event_date_treated_as_data_problem_not_alert(self):
        c = _candidate(dns_score=97, event_date=None)
        with patch("soccer_alerts.datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            data = sa.notify_soccer_candidates([c], date_str="2026-09-14")
        self.assertFalse(self.mock_send.called)

    def test_changed_kickoff_time_is_picked_up_on_a_later_run(self):
        """The exact regression case: a fixture postponed/rescheduled to
        a later kickoff must resume alerting normally once its
        event_date reflects the new (future) time - the kickoff check
        always re-reads event_date fresh off the current candidate, it
        never caches a stale verdict."""
        past = (NOW - timedelta(hours=2)).isoformat()
        c_stale = _candidate(dns_score=97, event_date=past, dabble_live=True, player_id="p1", fixture_id="f1")
        with patch("soccer_alerts.datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            sa.notify_soccer_candidates([c_stale], date_str="2026-09-14")
        self.assertFalse(self.mock_send.called, "should not have alerted on the stale past kickoff time")

        rescheduled = (NOW + timedelta(hours=5)).isoformat()
        c_rescheduled = _candidate(dns_score=97, event_date=rescheduled, dabble_live=True,
                                    player_id="p1", fixture_id="f1")
        with patch("soccer_alerts.datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            sa.notify_soccer_candidates([c_rescheduled], date_str="2026-09-14")
        self.assertTrue(self.mock_send.called, "a corrected future kickoff time must resume normal alerting")

    def test_kickoff_status_helper_directly(self):
        future = (NOW + timedelta(hours=1)).isoformat()
        past = (NOW - timedelta(hours=1)).isoformat()
        self.assertEqual(sa._kickoff_status({"event_date": future}, NOW_ISO)[0], "upcoming")
        self.assertEqual(sa._kickoff_status({"event_date": past}, NOW_ISO)[0], "post_kickoff")
        self.assertEqual(sa._kickoff_status({"event_date": None}, NOW_ISO)[0], "unknown")


class TestOneAlertPerThresholdCrossing(unittest.TestCase):
    """item 3, 2026-09-14: a player crossing every send-eligible
    threshold in one scoring pass (e.g. jumping 50 -> 97) must produce
    exactly one Discord message, not one per threshold - found live: 8
    alert records (60/65/70/75/80/85/90/critical) all created in the
    same second for one real event."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch_dir = patch("soccer_alerts.ALERTS_DIR", self._tmp.name)
        self._patch_dir.start()
        self._patch_send = patch("soccer_alerts.send_discord_alert", return_value=True)
        self.mock_send = self._patch_send.start()
        self._future = (NOW + timedelta(hours=3)).isoformat()

    def tearDown(self):
        self._patch_send.stop()
        self._patch_dir.stop()
        self._tmp.cleanup()

    def _run(self, dns_score, confidence_score=70):
        c = _candidate(dns_score=dns_score, confidence_score=confidence_score, event_date=self._future)
        with patch("soccer_alerts.datetime") as mock_dt:
            mock_dt.now.return_value = NOW
            return sa.notify_soccer_candidates([c], date_str="2026-09-14")

    def test_crossing_60_through_90_in_one_pass_sends_exactly_one_message(self):
        data = self._run(dns_score=97)
        self.assertEqual(self.mock_send.call_count, 1, "60->97 in one pass must send exactly one Discord message")
        threshold_keys = [k for k in data if k.startswith("2026-09-14:soccer:") and not k.endswith(":critical")]
        self.assertEqual(len(threshold_keys), len(sa.ALERT_THRESHOLDS),
                          "every threshold's own record/dedup key must still be written for calibration")

    def test_sent_alert_is_for_the_highest_send_eligible_threshold(self):
        self._run(dns_score=97)
        sent_record = self.mock_send.call_args[0][0]
        self.assertEqual(sent_record["alert_type"], max(sa.DISCORD_SEND_THRESHOLDS))

    def test_jump_past_a_non_sendable_max_threshold_still_sends_the_highest_sendable_one(self):
        """Crossing straight to 97 crosses 90 too (not in
        DISCORD_SEND_THRESHOLDS) - the numeric max of `crossed` must
        never suppress the real send for 85."""
        self._run(dns_score=97)
        self.assertEqual(self.mock_send.call_count, 1)
        sent_record = self.mock_send.call_args[0][0]
        self.assertEqual(sent_record["alert_type"], 85)
        self.assertNotEqual(sent_record["alert_type"], 90, "90 is not Discord-send-eligible and must never be sent")

    def test_later_rise_to_a_new_higher_threshold_sends_again(self):
        self._run(dns_score=76)  # crosses 75 -> 1 message
        self.assertEqual(self.mock_send.call_count, 1)
        self._run(dns_score=90)  # same fixture/player, now crosses 85 too -> 1 more message
        self.assertEqual(self.mock_send.call_count, 2)

    def test_same_score_again_sends_nothing_new(self):
        self._run(dns_score=76)
        self.assertEqual(self.mock_send.call_count, 1)
        self._run(dns_score=76)
        self.assertEqual(self.mock_send.call_count, 1, "no new threshold crossed - must not resend")

    def test_no_send_eligible_threshold_crossed_sends_nothing(self):
        # crosses 60/65/70 only - none in DISCORD_SEND_THRESHOLDS
        self._run(dns_score=72)
        self.assertEqual(self.mock_send.call_count, 0)


class TestGradeAlerts(unittest.TestCase):
    """item 4, 2026-09-14: grade_alerts was a stub that never actually
    determined an outcome and was never called anywhere in the pipeline.
    Real ESPN lookups always injected (get_match_squad_fn/find_event_fn/
    match_espn_team_id_fn) - no real network."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch_dir = patch("soccer_alerts.ALERTS_DIR", self._tmp.name)
        self._patch_dir.start()

    def tearDown(self):
        self._patch_dir.stop()
        self._tmp.cleanup()

    def _seed(self, record, key="rec1", date_str="2026-09-14"):
        data = {key: record}
        sa._save_alerts(date_str, data)
        return date_str

    def _base_record(self, **overrides):
        r = {
            "player_id": "p1", "player_name": "Test Player", "normalized_name": "test player",
            "team": "TST", "league_code": "EPL", "fixture_id": "f1",
            "game_start": "2026-09-14T15:00:00.000Z", "graded_at": None,
            "official_started": None, "outcome": None, "long_term_injury": False,
            "transfermarkt_injury_since": None,
        }
        r.update(overrides)
        return r

    FOUND_EVENT = {"id": "ev1", "status": {"type": {"name": "STATUS_FULL_TIME"}}}
    POSTPONED_EVENT = {"id": "ev1", "status": {"type": {"name": "STATUS_POSTPONED"}}}

    def _grade(self, date_str, squad=None, event=FOUND_EVENT, team_id="t1"):
        sa.grade_alerts(
            date_str,
            get_match_squad_fn=lambda lc, eid: (squad or {}),
            find_event_fn=lambda lc, tid, d: event,
            match_espn_team_id_fn=lambda lc, team: team_id,
        )
        return sa._load_alerts(date_str)

    def test_started_outcome(self):
        date_str = self._seed(self._base_record())
        data = self._grade(date_str, squad={"test player": {"starter": True, "active": True}})
        self.assertEqual(data["rec1"]["outcome"], "started")
        self.assertTrue(data["rec1"]["official_started"])
        self.assertIsNotNone(data["rec1"]["graded_at"])

    def test_came_off_bench_outcome(self):
        date_str = self._seed(self._base_record())
        data = self._grade(date_str, squad={"test player": {"starter": False, "active": True}})
        self.assertEqual(data["rec1"]["outcome"], "came_off_bench")
        self.assertFalse(data["rec1"]["official_started"])

    def test_unused_sub_outcome(self):
        date_str = self._seed(self._base_record())
        data = self._grade(date_str, squad={"test player": {"starter": False, "active": False}})
        self.assertEqual(data["rec1"]["outcome"], "unused_sub")
        self.assertFalse(data["rec1"]["official_started"])

    def test_not_in_squad_outcome(self):
        date_str = self._seed(self._base_record())
        data = self._grade(date_str, squad={"someone else": {"starter": True, "active": True}})
        self.assertEqual(data["rec1"]["outcome"], "not_in_squad")
        self.assertFalse(data["rec1"]["official_started"])

    def test_match_postponed_is_a_positive_signal_not_an_absence_guess(self):
        date_str = self._seed(self._base_record())
        data = self._grade(date_str, squad={}, event=self.POSTPONED_EVENT)
        self.assertEqual(data["rec1"]["outcome"], "match_postponed")
        self.assertIsNone(data["rec1"]["official_started"])
        self.assertIsNotNone(data["rec1"]["graded_at"])

    def test_no_event_found_leaves_record_ungraded(self):
        date_str = self._seed(self._base_record())
        data = self._grade(date_str, event=None)
        self.assertIsNone(data["rec1"]["graded_at"])
        self.assertIsNone(data["rec1"]["outcome"])

    def test_no_squad_posted_yet_leaves_record_ungraded(self):
        date_str = self._seed(self._base_record())
        data = self._grade(date_str, squad={})
        self.assertIsNone(data["rec1"]["graded_at"])

    def test_missing_required_fields_leaves_record_ungraded(self):
        date_str = self._seed(self._base_record(league_code=None))
        data = self._grade(date_str, squad={"test player": {"starter": True, "active": True}})
        self.assertIsNone(data["rec1"]["graded_at"])

    def test_already_graded_record_is_never_re_graded(self):
        date_str = self._seed(self._base_record(outcome="started", official_started=True,
                                                  graded_at="2026-09-14T20:00:00+00:00"))
        # Even if this call WOULD produce a different verdict, a settled record must not change.
        data = self._grade(date_str, squad={"test player": {"starter": False, "active": False}})
        self.assertEqual(data["rec1"]["outcome"], "started")
        self.assertEqual(data["rec1"]["graded_at"], "2026-09-14T20:00:00+00:00")

    def test_last_state_and_stale_kickoff_keys_are_never_touched(self):
        date_str = "2026-09-14"
        data = {
            "_last_state:p1:f1": {"dns_score": 80},
            f"{sa.STALE_KICKOFF_KEY_PREFIX}p1:f1": {"reason": "still_live_past_kickoff"},
            "rec1": self._base_record(),
        }
        sa._save_alerts(date_str, data)
        result = self._grade(date_str, squad={"test player": {"starter": True, "active": True}})
        self.assertEqual(result["_last_state:p1:f1"], {"dns_score": 80})
        self.assertEqual(result[f"{sa.STALE_KICKOFF_KEY_PREFIX}p1:f1"], {"reason": "still_live_past_kickoff"})

    def test_long_term_injury_flagged_when_since_predates_match_by_over_14_days(self):
        date_str = self._seed(self._base_record(transfermarkt_injury_since="2026-08-01T00:00:00+00:00"))
        data = self._grade(date_str, squad={"test player": {"starter": False, "active": False}})
        self.assertTrue(data["rec1"]["long_term_injury"])

    def test_recent_injury_not_flagged_as_long_term(self):
        date_str = self._seed(self._base_record(transfermarkt_injury_since="2026-09-10T00:00:00+00:00"))
        data = self._grade(date_str, squad={"test player": {"starter": False, "active": False}})
        self.assertFalse(data["rec1"]["long_term_injury"])

    def test_no_injury_since_is_not_long_term(self):
        date_str = self._seed(self._base_record(transfermarkt_injury_since=None))
        data = self._grade(date_str, squad={"test player": {"starter": True, "active": True}})
        self.assertFalse(data["rec1"]["long_term_injury"])


if __name__ == "__main__":
    unittest.main()
