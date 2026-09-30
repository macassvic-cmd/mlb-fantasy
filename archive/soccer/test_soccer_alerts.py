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

import json
import os
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


class TestSendDiscordAlertRedactsErrors(unittest.TestCase):
    """2026-09-15 security fix: send_discord_alert's own except block must
    never persist str(e) - a requests exception commonly embeds the full
    webhook URL. Exercises send_discord_alert directly (requests.post
    mocked to raise), not the higher-level notify_soccer_candidates
    wrapper other test classes mock send_discord_alert itself for."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch_path = patch("discord_health.HEALTH_PATH", os.path.join(self._tmp.name, "health.json"))
        self._patch_path.start()
        self._patch_env = patch.dict("os.environ", {"DISCORD_SOCCER_DNS_WEBHOOK_URL":
                                                      "https://discord.com/api/webhooks/1/faketoken"})
        self._patch_env.start()

    def tearDown(self):
        self._patch_env.stop()
        self._patch_path.stop()
        self._tmp.cleanup()

    def test_connection_error_never_leaks_the_webhook_url_into_health(self):
        import discord_health
        import requests

        record = {
            "player_name": "Test Player", "team": "TST", "opponent": "TST @ OPP",
            "dns_score": 80, "confidence_score": 70, "urgency_score": 0,
            "dabble_live": True, "official_status": "not_yet_posted",
            "dabble_markets": [], "top_reasons": [], "game_start": None,
        }
        webhook_url = "https://discord.com/api/webhooks/1/faketoken"
        with patch("requests.post", side_effect=requests.exceptions.ConnectionError(
                f"Failed to establish a new connection: url: {webhook_url}")):
            result = sa.send_discord_alert(record, "DNS ALERT")

        self.assertFalse(result)
        health = discord_health.get_health()
        self.assertNotIn(webhook_url, json.dumps(health))
        self.assertNotIn("faketoken", json.dumps(health))
        self.assertIn("ConnectionError", health["last_error"])


class TestBackfillGradingFields(unittest.TestCase):
    """item 3, 2026-09-15: backfill league_code/normalized_name onto
    pre-item-4-schema alert records using ONLY data already stored
    elsewhere (the live snapshot file), never a guess."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch_dir = patch("soccer_alerts.ALERTS_DIR", self._tmp.name)
        self._patch_dir.start()
        self._snap_tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._patch_dir.stop()
        self._tmp.cleanup()
        self._snap_tmp.cleanup()

    def _old_record(self, **overrides):
        r = {
            "player_id": "p1", "player_name": "Test Player", "team": "TST",
            "fixture_id": "f1", "official_started": None, "graded_at": None,
        }
        r.update(overrides)
        return r

    def _seed_alerts(self, date_str, records):
        data = dict(records)
        sa._save_alerts(date_str, data)

    def _seed_snapshot(self, date_str, snapshot):
        path = os.path.join(self._snap_tmp.name, f"{date_str}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)

    def _backfill(self, date_str):
        return sa.backfill_grading_fields(date_str, snapshots_dir=self._snap_tmp.name)

    def test_backfills_league_code_and_normalized_name_from_snapshot(self):
        date_str = "2026-09-14"
        self._seed_alerts(date_str, {"rec1": self._old_record()})
        self._seed_snapshot(date_str, {
            "pid:p1": {"fixture_id": "f1", "latest": {"league_code": "EPL", "normalized_name": "test player"}},
        })
        n = self._backfill(date_str)
        data = sa._load_alerts(date_str)
        self.assertEqual(n, 1)
        self.assertEqual(data["rec1"]["league_code"], "EPL")
        self.assertEqual(data["rec1"]["normalized_name"], "test player")

    def test_falls_back_to_name_team_key_when_no_player_id(self):
        date_str = "2026-09-14"
        self._seed_alerts(date_str, {"rec1": self._old_record(player_id=None)})
        self._seed_snapshot(date_str, {
            "name:test player:TST": {"fixture_id": "f1", "latest": {"league_code": "LLG"}},
        })
        n = self._backfill(date_str)
        data = sa._load_alerts(date_str)
        self.assertEqual(n, 1)
        self.assertEqual(data["rec1"]["league_code"], "LLG")

    def test_fixture_id_mismatch_is_left_ungraded(self):
        date_str = "2026-09-14"
        self._seed_alerts(date_str, {"rec1": self._old_record(fixture_id="f1")})
        self._seed_snapshot(date_str, {
            "pid:p1": {"fixture_id": "DIFFERENT_FIXTURE", "latest": {"league_code": "EPL"}},
        })
        n = self._backfill(date_str)
        data = sa._load_alerts(date_str)
        self.assertEqual(n, 0)
        self.assertNotIn("league_code", data["rec1"])

    def test_no_matching_snapshot_entry_is_left_ungraded(self):
        date_str = "2026-09-14"
        self._seed_alerts(date_str, {"rec1": self._old_record()})
        self._seed_snapshot(date_str, {"pid:someone_else": {"fixture_id": "f1", "latest": {}}})
        n = self._backfill(date_str)
        data = sa._load_alerts(date_str)
        self.assertEqual(n, 0)
        self.assertNotIn("league_code", data["rec1"])

    def test_snapshot_entry_missing_league_code_is_left_ungraded(self):
        date_str = "2026-09-14"
        self._seed_alerts(date_str, {"rec1": self._old_record()})
        self._seed_snapshot(date_str, {"pid:p1": {"fixture_id": "f1", "latest": {}}})
        n = self._backfill(date_str)
        self.assertEqual(n, 0)

    def test_no_snapshot_file_at_all_leaves_everything_ungraded(self):
        date_str = "2026-09-14"
        self._seed_alerts(date_str, {"rec1": self._old_record()})
        n = self._backfill(date_str)
        self.assertEqual(n, 0)

    def test_already_new_schema_record_is_skipped_not_overwritten(self):
        date_str = "2026-09-14"
        self._seed_alerts(date_str, {"rec1": self._old_record(league_code="MLS", normalized_name="already set")})
        self._seed_snapshot(date_str, {
            "pid:p1": {"fixture_id": "f1", "latest": {"league_code": "EPL"}},
        })
        n = self._backfill(date_str)
        data = sa._load_alerts(date_str)
        self.assertEqual(n, 0)
        self.assertEqual(data["rec1"]["league_code"], "MLS", "an existing league_code must never be overwritten")

    def test_last_state_and_stale_kickoff_keys_are_never_touched(self):
        date_str = "2026-09-14"
        self._seed_alerts(date_str, {
            "_last_state:p1:f1": {"dns_score": 80},
            f"{sa.STALE_KICKOFF_KEY_PREFIX}p1:f1": {"reason": "x"},
            "rec1": self._old_record(),
        })
        self._seed_snapshot(date_str, {"pid:p1": {"fixture_id": "f1", "latest": {"league_code": "EPL"}}})
        self._backfill(date_str)
        data = sa._load_alerts(date_str)
        self.assertEqual(data["_last_state:p1:f1"], {"dns_score": 80})
        self.assertEqual(data[f"{sa.STALE_KICKOFF_KEY_PREFIX}p1:f1"], {"reason": "x"})


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


class TestClassifyAlert(unittest.TestCase):
    """2026-09-15 fix: PREDICTION (alerted before official confirmation
    and before kickoff) vs CONFIRMATION (alerted after official_status
    was already confirmed_not_starting, or after kickoff) - found live
    that 32 of 33 graded 2026-09-14 alerts were confirmations, not
    predictions, and the plain win/loss record had been conflating the
    two."""

    def test_already_confirmed_not_starting_is_a_confirmation(self):
        self.assertEqual(sa._classify_alert("confirmed_not_starting", 120), "confirmation")

    def test_not_yet_confirmed_and_before_kickoff_is_a_prediction(self):
        self.assertEqual(sa._classify_alert("not_yet_posted", 120), "prediction")

    def test_at_or_after_kickoff_is_a_confirmation_even_if_not_yet_confirmed(self):
        self.assertEqual(sa._classify_alert("not_yet_posted", 0), "confirmation")
        self.assertEqual(sa._classify_alert("not_yet_posted", -30), "confirmation")

    def test_build_alert_record_stores_the_classification(self):
        future_iso = "2026-09-20T15:00:00.000Z"
        candidate = {
            "dabble_player_id": "p1", "player_name": "Test", "team": "TST", "matchup": "TST @ OPP",
            "fixture_id": "f1", "event_date": future_iso, "dns_score": 80, "confidence_score": 70,
            "urgency_score": 0, "official_status": "not_yet_posted", "dabble_status_raw": None,
            "props": [], "top_reasons": [], "transfermarkt_injury": None,
        }
        now_iso = "2026-09-14T00:00:00+00:00"
        record = sa._build_alert_record(candidate, 75, "2026-09-14", now_iso)
        self.assertEqual(record["alert_classification"], "prediction")

        candidate["official_status"] = "confirmed_not_starting"
        record2 = sa._build_alert_record(candidate, sa.CRITICAL_KEY, "2026-09-14", now_iso)
        self.assertEqual(record2["alert_classification"], "confirmation")


class TestGradingSummary(unittest.TestCase):
    """item 2/3, 2026-09-15: grading_summary/print_grading_report split
    graded records by alert_classification and bucket PREDICTION wins by
    lead time."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch_dir = patch("soccer_alerts.ALERTS_DIR", self._tmp.name)
        self._patch_dir.start()

    def tearDown(self):
        self._patch_dir.stop()
        self._tmp.cleanup()

    def _record(self, classification, outcome, alerted_at=None, game_start=None, long_term_injury=False):
        return {
            "alert_classification": classification, "outcome": outcome, "official_started": None,
            "graded_at": "2026-09-14T20:00:00+00:00", "long_term_injury": long_term_injury,
            "alerted_at": alerted_at, "game_start": game_start,
        }

    def test_splits_prediction_and_confirmation(self):
        date_str = "2026-09-14"
        sa._save_alerts(date_str, {
            "r1": self._record("prediction", "not_in_squad"),
            "r2": self._record("confirmation", "not_in_squad"),
            "r3": self._record("confirmation", "came_off_bench"),
        })
        summary = sa.grading_summary(date_str)
        self.assertEqual(summary["prediction"]["n"], 1)
        self.assertEqual(summary["confirmation"]["n"], 2)

    def test_ungraded_records_are_excluded(self):
        date_str = "2026-09-14"
        r = self._record("prediction", "not_in_squad")
        r["graded_at"] = None
        sa._save_alerts(date_str, {"r1": r})
        summary = sa.grading_summary(date_str)
        self.assertEqual(summary["prediction"]["n"], 0)

    def test_win_loss_matches_existing_rule_unchanged(self):
        date_str = "2026-09-14"
        sa._save_alerts(date_str, {
            "r1": self._record("prediction", "not_in_squad"),
            "r2": self._record("prediction", "came_off_bench"),
            "r3": self._record("prediction", "unused_sub"),
            "r4": self._record("prediction", "started"),
        })
        summary = sa.grading_summary(date_str)
        self.assertEqual(summary["prediction"]["win"], 3)
        self.assertEqual(summary["prediction"]["loss"], 1)

    def test_long_term_injury_excluded_from_win_loss(self):
        date_str = "2026-09-14"
        sa._save_alerts(date_str, {
            "r1": self._record("prediction", "not_in_squad", long_term_injury=True),
        })
        summary = sa.grading_summary(date_str)
        self.assertEqual(summary["prediction"]["win"], 0)
        self.assertEqual(summary["prediction"]["long_term_injury"], 1)

    def test_prediction_win_lead_time_buckets(self):
        date_str = "2026-09-14"
        sa._save_alerts(date_str, {
            "r1": self._record("prediction", "not_in_squad",
                                alerted_at="2026-09-10T00:00:00+00:00", game_start="2026-09-14T00:00:00+00:00"),
            "r2": self._record("prediction", "not_in_squad",
                                alerted_at="2026-09-13T20:00:00+00:00", game_start="2026-09-14T00:00:00+00:00"),
            "r3": self._record("prediction", "not_in_squad",
                                alerted_at="2026-09-13T23:00:00+00:00", game_start="2026-09-14T00:00:00+00:00"),
        })
        summary = sa.grading_summary(date_str)
        buckets = summary["prediction"]["win_lead_time_buckets"]
        self.assertEqual(buckets["24h+"], 1)
        self.assertEqual(buckets["2-24h"], 1)
        self.assertEqual(buckets["<2h"], 1)

    def test_confirmation_side_has_no_lead_time_buckets(self):
        date_str = "2026-09-14"
        sa._save_alerts(date_str, {"r1": self._record("confirmation", "not_in_squad")})
        summary = sa.grading_summary(date_str)
        self.assertNotIn("win_lead_time_buckets", summary["confirmation"])

    def test_old_record_without_stored_classification_falls_back_to_computing_it(self):
        date_str = "2026-09-14"
        old_style = {
            "outcome": "not_in_squad", "official_started": False, "graded_at": "2026-09-14T20:00:00+00:00",
            "long_term_injury": False, "official_status": "confirmed_not_starting",
            "minutes_to_game": 100, "alerted_at": None, "game_start": None,
        }
        sa._save_alerts(date_str, {"r1": old_style})
        summary = sa.grading_summary(date_str)
        self.assertEqual(summary["confirmation"]["n"], 1, "old records lacking alert_classification "
                                                            "must still classify correctly")


class TestGradeAlertsRecent(unittest.TestCase):
    """item 4, 2026-09-15: grade_alerts_recent sweeps the last N days
    every call, not just yesterday - grade_alerts alone never retries a
    date once "yesterday" moves past it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch_dir = patch("soccer_alerts.ALERTS_DIR", self._tmp.name)
        self._patch_dir.start()

    def tearDown(self):
        self._patch_dir.stop()
        self._tmp.cleanup()

    def test_grades_a_date_several_days_back_not_only_yesterday(self):
        # 3 days before "today" (2026-09-14) - grade_alerts alone (called
        # only for "yesterday") would never reach this date at all.
        old_date = "2026-09-11"
        sa._save_alerts(old_date, {"r1": {
            "player_id": "p1", "player_name": "Test", "team": "TST", "league_code": "EPL",
            "normalized_name": "test", "fixture_id": "f1", "graded_at": None, "official_started": None,
        }})
        with patch("soccer_alerts.grade_alerts") as mock_grade:
            def fake_grade(d):
                data = sa._load_alerts(d)
                for k, v in data.items():
                    if not k.startswith("_") and v.get("graded_at") is None:
                        v["graded_at"] = "2026-09-14T00:00:00+00:00"
                        v["outcome"] = "not_in_squad"
                sa._save_alerts(d, data)
            mock_grade.side_effect = fake_grade
            n = sa.grade_alerts_recent(days=7, as_of_date="2026-09-14")
        self.assertEqual(n, 1)
        self.assertIsNotNone(sa._load_alerts(old_date)["r1"]["graded_at"])

    def test_a_date_with_no_alerts_file_is_skipped_cheaply(self):
        n = sa.grade_alerts_recent(days=7, as_of_date="2026-09-14")
        self.assertEqual(n, 0)

    def test_one_dates_failure_does_not_block_the_others(self):
        good_date = "2026-09-12"
        sa._save_alerts(good_date, {"r1": {"graded_at": None}})
        with patch("soccer_alerts.grade_alerts", side_effect=[Exception("boom")] * 2 + [None] * 5):
            n = sa.grade_alerts_recent(days=7, as_of_date="2026-09-14")
        # no crash raised - non-fatal per-date handling confirmed by reaching here
        self.assertIsInstance(n, int)


class TestBuildEmbedKickoffDisplay(unittest.TestCase):
    """2026-09-16 Hinshelwood item 4 - the Discord alert body was showing
    raw UTC ISO ('2026-09-16T19:00:00.000Z') instead of Pacific time."""

    def _record(self, game_start):
        return {
            "player_name": "Jack Hinshelwood", "team": "BHA", "opponent": "BHA @ EVE",
            "dns_score": 90, "confidence_score": 70, "urgency_score": 20,
            "dabble_live": True, "official_status": "not_yet_posted",
            "dabble_markets": [], "top_reasons": [], "game_start": game_start,
        }

    def test_embed_description_shows_pacific_time_not_raw_iso(self):
        embed = sa._build_embed(self._record("2026-09-16T19:00:00.000Z"), "HIGH PRIORITY")
        self.assertNotIn("T19:00:00.000Z", embed["description"])
        self.assertIn("PDT", embed["description"])

    def test_embed_description_falls_back_to_time_tbd_when_missing(self):
        embed = sa._build_embed(self._record(None), "HIGH PRIORITY")
        self.assertIn("time TBD", embed["description"])


class TestHardOutResearchBlock(unittest.TestCase):
    """2026-09-16 Hinshelwood item 3 - the research block behind a
    hard_out alert (Discord field + candidate/alert record field)."""

    def _rb(self, **overrides):
        rb = {
            "rotowire_url": "https://www.rotowire.com/soccer/player/jack-hinshelwood-1",
            "rotowire_status_tag": "Out", "rotowire_injury": "Hip",
            "rotowire_est_return": "TBD", "rotowire_status_since": "2026-09-10T00:00:00+00:00",
            "transfermarkt": None, "last_appeared_date": "2026-08-23",
            "days_since_last_appearance": 24, "team_last_3_fixtures": [
                {"date": "2026-08-30", "status": "absent"}, {"date": "2026-09-05", "status": "absent"},
                {"date": "2026-09-13", "status": "absent"},
            ], "corroborated_by": ["absent from most recent squad"], "conflict": None,
        }
        rb.update(overrides)
        return rb

    def test_research_block_includes_the_asked_for_fields(self):
        text = sa._format_research_block(self._rb())
        self.assertIn("Out - Hip", text)
        self.assertIn("rotowire.com", text)
        self.assertIn("2026-08-23", text)
        self.assertIn("24 days ago", text)
        self.assertIn("absent, 2026-09-05 absent, 2026-09-13 absent", text)
        self.assertIn("Corroborated by 1 source", text)

    def test_research_block_shows_conflict_instead_of_corroboration_line(self):
        text = sa._format_research_block(self._rb(conflict="started 2026-09-13 after hard_out first seen 2026-09-10"))
        self.assertIn("CONFLICT", text)
        self.assertNotIn("Corroborated by", text)

    def test_research_block_shows_transfermarkt_when_present(self):
        text = sa._format_research_block(self._rb(transfermarkt={
            "reason": "Hip injury", "since": "Aug 26, 2026", "expected_return": "late Sept"}))
        self.assertIn("Transfermarkt: Hip injury", text)
        self.assertIn("late Sept", text)

    def test_build_embed_includes_a_rotowire_field_for_hard_out_alerts(self):
        record = self._record("2026-09-16T19:00:00.000Z")
        record["research_block"] = self._rb()
        embed = sa._build_embed(record, "HIGH PRIORITY")
        rw_field = next((f for f in embed["fields"] if f["name"] == "RotoWire"), None)
        self.assertIsNotNone(rw_field, "hard_out alerts must carry a RotoWire research field")
        self.assertIn("Corroborated by", rw_field["value"])

    def test_build_embed_has_no_rotowire_field_when_no_research_block(self):
        embed = sa._build_embed(self._record("2026-09-16T19:00:00.000Z"), "HIGH PRIORITY")
        self.assertIsNone(next((f for f in embed["fields"] if f["name"] == "RotoWire"), None))

    def _record(self, game_start):
        return {
            "player_name": "Jack Hinshelwood", "team": "BHA", "opponent": "BHA @ EVE",
            "dns_score": 90, "confidence_score": 70, "urgency_score": 20,
            "dabble_live": True, "official_status": "not_yet_posted",
            "dabble_markets": [], "top_reasons": [], "game_start": game_start,
        }


if __name__ == "__main__":
    unittest.main()
