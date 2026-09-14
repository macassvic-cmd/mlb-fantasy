"""Unit tests for dnp_alerts.py. Run with: python -m unittest test_dnp_alerts -v

All Discord POSTs are mocked - no real webhook is ever hit. Alert storage
is redirected to a throwaway directory per test via patching ALERTS_DIR.
"""

import json
import os
import shutil
import unittest
from unittest.mock import patch, MagicMock

import dnp_alerts

TEST_ALERTS_DIR = os.path.join("data", "_test_dnp_alerts")


def _candidate(player_id=111, game_id=900001, dnp_score=76, confidence_score=60,
               urgency_score=10, status="not_yet_posted", props=None):
    return {
        "player_id": player_id, "player_name": "Test Player", "team": "NYY", "opponent": "BOS",
        "game_id": game_id, "game_start": "2026-09-13T23:05:00+00:00",
        "dnp_score": dnp_score, "confidence_score": confidence_score, "urgency_score": urgency_score,
        "projected_start_status": status,
        "props": props if props is not None else [{"market": "Hits", "line": 0.5, "prop_id": "abc123"}],
        "top_reasons": ["some reason"],
    }


class AlertsTestCase(unittest.TestCase):
    def setUp(self):
        self._dir_patch = patch("dnp_alerts.ALERTS_DIR", TEST_ALERTS_DIR)
        self._dir_patch.start()

    def tearDown(self):
        self._dir_patch.stop()
        if os.path.isdir(TEST_ALERTS_DIR):
            shutil.rmtree(TEST_ALERTS_DIR)


class TestThresholdCrossing(AlertsTestCase):
    def test_crossing_75_from_below_creates_alert_and_sends(self):
        with patch("dnp_alerts.requests.post") as mock_post, \
             patch.dict(os.environ, {"DISCORD_DNP_WEBHOOK_URL": "https://example.invalid/webhook"}):
            mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
            data = dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=76, confidence_score=60)],
                                                        date_str="2099-01-01")
        key75 = dnp_alerts._dedup_key("2099-01-01", 111, 900001, 75)
        self.assertIn(key75, data)
        mock_post.assert_called_once()

    def test_score_below_70_creates_no_alert(self):
        with patch("dnp_alerts.requests.post") as mock_post:
            data = dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=40)], date_str="2099-01-01")
        alert_keys = [k for k in data if not k.startswith("_last_score:")]
        self.assertEqual(len(alert_keys), 0)
        mock_post.assert_not_called()

    def test_watch_tier_70_74_tracked_but_no_discord_send(self):
        with patch("dnp_alerts.requests.post") as mock_post:
            data = dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=72, confidence_score=90)],
                                                        date_str="2099-01-01")
        key70 = dnp_alerts._dedup_key("2099-01-01", 111, 900001, 70)
        self.assertIn(key70, data, "70 should be tracked for calibration even though it doesn't send")
        mock_post.assert_not_called()


class TestDedupeAndEscalation(AlertsTestCase):
    def test_no_repeat_alert_within_same_tier(self):
        """73 -> 74: no new alert (never crossed 75); repeating the same
        score again must not re-fire anything already sent."""
        with patch("dnp_alerts.requests.post") as mock_post:
            dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=73)], date_str="2099-01-01")
            data = dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=74)], date_str="2099-01-01")
        alert_keys = [k for k in data if not k.startswith("_last_score:")]
        self.assertEqual(sorted(t for k in alert_keys for t in [data[k]["threshold"]]), [60, 65, 70])

    def test_74_to_76_sends_75_then_76_to_79_does_not_repeat(self):
        with patch("dnp_alerts.requests.post") as mock_post, \
             patch.dict(os.environ, {"DISCORD_DNP_WEBHOOK_URL": "https://example.invalid/webhook"}):
            mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
            dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=74, confidence_score=60)], date_str="2099-01-01")
            self.assertEqual(mock_post.call_count, 0)
            dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=76, confidence_score=60)], date_str="2099-01-01")
            self.assertEqual(mock_post.call_count, 1, "crossing 75 must send exactly one alert")
            dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=79, confidence_score=60)], date_str="2099-01-01")
            self.assertEqual(mock_post.call_count, 1, "staying within 75-84 must not re-send")

    def test_79_to_86_sends_85_escalation_not_75_again(self):
        with patch("dnp_alerts.requests.post") as mock_post, \
             patch.dict(os.environ, {"DISCORD_DNP_WEBHOOK_URL": "https://example.invalid/webhook"}):
            mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
            dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=79, confidence_score=70)], date_str="2099-01-01")
            call_count_before = mock_post.call_count
            data = dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=86, confidence_score=70)],
                                                        date_str="2099-01-01")
            self.assertEqual(mock_post.call_count, call_count_before + 1, "only the NEW 85 crossing should send")
        key85 = dnp_alerts._dedup_key("2099-01-01", 111, 900001, 85)
        self.assertIn(key85, data)


class TestConfidenceGateAndDabbleLive(AlertsTestCase):
    def test_high_score_low_confidence_tracks_but_does_not_send(self):
        with patch("dnp_alerts.requests.post") as mock_post, \
             patch.dict(os.environ, {"DISCORD_DNP_WEBHOOK_URL": "https://example.invalid/webhook"}):
            data = dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=80, confidence_score=20)],
                                                        date_str="2099-01-01")
        key75 = dnp_alerts._dedup_key("2099-01-01", 111, 900001, 75)
        self.assertIn(key75, data, "the crossing is still tracked for calibration")
        mock_post.assert_not_called()

    def test_dabble_live_is_true_for_every_created_record(self):
        # A candidate only ever exists here because it was just seen live
        # on the board - see module docstring.
        with patch("dnp_alerts.requests.post"):
            data = dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=76, confidence_score=60)],
                                                        date_str="2099-01-01")
        key75 = dnp_alerts._dedup_key("2099-01-01", 111, 900001, 75)
        self.assertTrue(data[key75]["dabble_live"])

    def test_dabble_removal_detected_on_next_pass(self):
        with patch("dnp_alerts.requests.post"), \
             patch.dict(os.environ, {"DISCORD_DNP_WEBHOOK_URL": "https://example.invalid/webhook"}):
            dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=76, confidence_score=60)], date_str="2099-01-01")
            data = dnp_alerts.notify_dabble_candidates([], date_str="2099-01-01")  # player no longer on the board
        key75 = dnp_alerts._dedup_key("2099-01-01", 111, 900001, 75)
        self.assertIsNotNone(data[key75]["dabble_removed_at"])


class TestConfirmedNonStarterOverride(AlertsTestCase):
    def test_confirmed_not_starting_sends_critical_regardless_of_confidence(self):
        with patch("dnp_alerts.requests.post") as mock_post, \
             patch.dict(os.environ, {"DISCORD_DNP_WEBHOOK_URL": "https://example.invalid/webhook"}):
            mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
            data = dnp_alerts.notify_dabble_candidates(
                [_candidate(dnp_score=97, confidence_score=5, status="confirmed_not_starting")],
                date_str="2099-01-01",
            )
        crit_key = dnp_alerts._dedup_key("2099-01-01", 111, 900001, dnp_alerts.CRITICAL_KEY)
        self.assertIn(crit_key, data)
        self.assertTrue(any(c.kwargs.get("json", {}) or c.args for c in mock_post.call_args_list))

    def test_critical_fires_only_once(self):
        with patch("dnp_alerts.requests.post") as mock_post, \
             patch.dict(os.environ, {"DISCORD_DNP_WEBHOOK_URL": "https://example.invalid/webhook"}):
            mock_post.return_value = MagicMock(status_code=204, raise_for_status=lambda: None)
            dnp_alerts.notify_dabble_candidates(
                [_candidate(dnp_score=97, status="confirmed_not_starting")], date_str="2099-01-01")
            dnp_alerts.notify_dabble_candidates(
                [_candidate(dnp_score=97, status="confirmed_not_starting")], date_str="2099-01-01")
        # one CRITICAL send + one 90/85/... threshold send on the first pass only;
        # the point under test is CRITICAL itself never double-fires.
        crit_calls = [c for c in mock_post.call_args_list
                      if "CONFIRMED NON-STARTER" in json_of(c)]
        self.assertEqual(len(crit_calls), 1)


def json_of(call):
    kwargs = call.kwargs
    return json.dumps(kwargs.get("json", {}))


class TestFailedWebhook(AlertsTestCase):
    def test_failed_post_returns_false_and_does_not_raise(self):
        with patch("dnp_alerts.requests.post", side_effect=ConnectionError("boom")), \
             patch.dict(os.environ, {"DISCORD_DNP_WEBHOOK_URL": "https://example.invalid/webhook"}):
            ok = dnp_alerts.send_discord_alert(
                dnp_alerts._build_alert_record(_candidate(), 75, "2099-01-01", "2099-01-01T00:00:00+00:00"),
                "DNP ALERT",
            )
        self.assertFalse(ok)

    def test_notify_completes_even_when_every_send_fails(self):
        with patch("dnp_alerts.requests.post", side_effect=ConnectionError("boom")), \
             patch.dict(os.environ, {"DISCORD_DNP_WEBHOOK_URL": "https://example.invalid/webhook"}):
            data = dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=90, confidence_score=90)],
                                                        date_str="2099-01-01")
        key85 = dnp_alerts._dedup_key("2099-01-01", 111, 900001, 85)
        self.assertIn(key85, data, "the alert record must still persist even if the Discord send failed")

    def test_missing_webhook_env_var_is_a_silent_noop(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCORD_DNP_WEBHOOK_URL", None)
            with patch("dnp_alerts.requests.post") as mock_post:
                ok = dnp_alerts.send_discord_alert(
                    dnp_alerts._build_alert_record(_candidate(), 75, "2099-01-01", "2099-01-01T00:00:00+00:00"),
                    "DNP ALERT",
                )
        self.assertFalse(ok)
        mock_post.assert_not_called()


class TestAlertGrading(AlertsTestCase):
    def test_grade_alerts_populates_official_started(self):
        with patch("dnp_alerts.requests.post"):
            dnp_alerts.notify_dabble_candidates([_candidate(dnp_score=76, confidence_score=60)],
                                                 date_str="2099-01-01")

        def fake_game_data(game_pk):
            return {"home": {"players": []}, "away": {"players": [{"player_id": 111, "started": False}]}}

        with patch("dnp_alerts.get_game_start_data", side_effect=fake_game_data):
            dnp_alerts.grade_alerts("2099-01-01")

        data = dnp_alerts._load_alerts("2099-01-01")
        key75 = dnp_alerts._dedup_key("2099-01-01", 111, 900001, 75)
        self.assertEqual(data[key75]["official_started"], False)
        self.assertIsNotNone(data[key75]["graded_at"])


if __name__ == "__main__":
    unittest.main()
