"""Unit tests for soccer_daily_digest.py. Run with:
python -m unittest test_soccer_daily_digest -v

Isolation: every test patches DIGEST_DIR to a temp directory and mocks
_post_discord / soccer_adapter.score_dabble_soccer_board directly - no
real network call, no real data/soccer_daily_digest/ file is ever
touched, no real board file is read unless a test explicitly builds one
in a temp dir.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import soccer_daily_digest as digest


def _fake_candidate(name, dns=50, conf=50, urg=0, team="NEW"):
    return {
        "player_name": name, "team": team, "dabble_player_id": name.lower().replace(" ", "-"),
        "league_code": "EPL", "league_raw": "EPL", "matchup": "NEW @ LEE",
        "event_date": "2026-09-14T19:00:00.000Z", "fixture_id": "fx1",
        "dns_score": dns, "confidence_score": conf, "urgency_score": urg,
        "combined_priority": round(dns * 0.7 + urg * 0.3, 1),
        "dabble_status_raw": None, "dabble_status_normalized": None,
        "rotowire_status_raw": None, "rotowire_page_tag": None, "rotowire_injury": None,
        "transfermarkt_injury": None, "has_news": False, "official_status": "not_yet_posted",
        "prediction_consensus": "unknown", "predicted_start_sources": [], "predicted_xi_sources": [],
        "top_reasons": ["some reason"],
    }


class _TempDigestDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch_dir = patch.object(digest, "DIGEST_DIR", self._tmp.name)
        self._patch_dir.start()
        # _attempt_board_refresh would otherwise invoke the REAL external
        # Dabble board producer (confirmed present on this machine, hits
        # a real live API) - must never fire from a unit test.
        self._patch_refresh = patch.object(digest, "_attempt_board_refresh", return_value=False)
        self._patch_refresh.start()
        # soccer_dashboard.generate would otherwise overwrite the REAL
        # docs/soccer-dns.html on every test run - must never fire either.
        self._patch_dashboard = patch("soccer_dashboard.generate")
        self._patch_dashboard.start()
        # soccer_alerts.grade_alerts_recent (item 4, 2026-09-14, swept to
        # a 7-day window 2026-09-15) would otherwise load-and-resave up
        # to 7 real data/soccer_dns_alerts/<date>.json files on every
        # test run if any happen to exist - must never touch real
        # production alert data from a unit test.
        self._patch_grade = patch("soccer_alerts.grade_alerts_recent")
        self._patch_grade.start()

    def tearDown(self):
        self._patch_dir.stop()
        self._patch_refresh.stop()
        self._patch_dashboard.stop()
        self._patch_grade.stop()
        self._tmp.cleanup()


class TestDigestSendsOnce(_TempDigestDir):
    def _fresh_board_source(self, tmp_dir):
        path = os.path.join(tmp_dir, "board.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"sport": "soccer", "generated_at": datetime.now(timezone.utc).isoformat(), "props": []}, f)
        return path

    def test_sends_once_and_marks_delivered(self):
        with tempfile.TemporaryDirectory() as board_dir:
            source = self._fresh_board_source(board_dir)
            with patch("soccer_adapter.score_dabble_soccer_board", return_value=[_fake_candidate("Player A", dns=80)]), \
                 patch.object(digest, "_post_discord", return_value=(True, True, 204, None)) as mock_post:
                record = digest.run_daily_digest(source=source, date_str="2026-09-14")
                self.assertTrue(record["discord_delivered"])
                self.assertEqual(record["candidate_count"], 1)
                self.assertEqual(mock_post.call_count, 1)

                # Repeated same-day invocation without --force must NOT re-post.
                record2 = digest.run_daily_digest(source=source, date_str="2026-09-14")
                self.assertEqual(mock_post.call_count, 1, "a second same-day call must not duplicate delivery")
                self.assertEqual(record2, record)

    def test_force_resend_posts_again(self):
        with tempfile.TemporaryDirectory() as board_dir:
            source = self._fresh_board_source(board_dir)
            with patch("soccer_adapter.score_dabble_soccer_board", return_value=[_fake_candidate("Player A")]), \
                 patch.object(digest, "_post_discord", return_value=(True, True, 204, None)) as mock_post:
                digest.run_daily_digest(source=source, date_str="2026-09-14")
                digest.run_daily_digest(source=source, date_str="2026-09-14", force=True)
                self.assertEqual(mock_post.call_count, 2, "--force must resend even if already delivered")


class TestZeroCandidates(_TempDigestDir):
    def test_zero_candidates_still_sends_alive_message(self):
        with tempfile.TemporaryDirectory() as board_dir:
            path = os.path.join(board_dir, "board.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"sport": "soccer", "generated_at": datetime.now(timezone.utc).isoformat(), "props": []}, f)
            with patch("soccer_adapter.score_dabble_soccer_board", return_value=[]), \
                 patch.object(digest, "_post_discord", return_value=(True, True, 204, None)) as mock_post:
                record = digest.run_daily_digest(source=path, date_str="2026-09-14")
                self.assertTrue(record["discord_attempted"])
                self.assertTrue(record["discord_delivered"])
                self.assertEqual(record["candidate_count"], 0)
                kwargs = mock_post.call_args.kwargs
                self.assertIn("no qualifying candidates", kwargs.get("content", ""))


class TestStaleBoardWarning(_TempDigestDir):
    def test_stale_board_sends_warning_not_digest(self):
        with tempfile.TemporaryDirectory() as board_dir:
            path = os.path.join(board_dir, "board.json")
            old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"sport": "soccer", "generated_at": old_ts, "props": []}, f)
            with patch("soccer_adapter.score_dabble_soccer_board") as mock_score, \
                 patch.object(digest, "_post_discord", return_value=(True, True, 204, None)) as mock_post:
                record = digest.run_daily_digest(source=path, date_str="2026-09-14")
                mock_score.assert_not_called()
                self.assertIn("stale", mock_post.call_args.kwargs.get("content", "").lower())
                self.assertEqual(record["candidate_count"], 0)

    def test_missing_board_file_is_also_stale(self):
        with patch("soccer_adapter.score_dabble_soccer_board") as mock_score, \
             patch.object(digest, "_post_discord", return_value=(True, True, 204, None)) as mock_post:
            digest.run_daily_digest(source="/nonexistent/board.json", date_str="2026-09-14")
            mock_score.assert_not_called()
            self.assertIn("stale", mock_post.call_args.kwargs.get("content", "").lower())


class TestFailedDiscordRetryable(_TempDigestDir):
    def test_failed_send_leaves_undelivered_and_retries_without_force(self):
        with tempfile.TemporaryDirectory() as board_dir:
            path = os.path.join(board_dir, "board.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"sport": "soccer", "generated_at": datetime.now(timezone.utc).isoformat(), "props": []}, f)
            with patch("soccer_adapter.score_dabble_soccer_board", return_value=[_fake_candidate("Player A")]), \
                 patch.object(digest, "_post_discord", return_value=(True, False, 500, "server error")) as mock_post:
                record = digest.run_daily_digest(source=path, date_str="2026-09-14")
                self.assertTrue(record["discord_attempted"])
                self.assertFalse(record["discord_delivered"])
                self.assertEqual(record["discord_error"], "server error")

                # A second call the SAME day, still without --force, must retry
                # automatically - only a genuinely DELIVERED digest blocks a resend.
                digest.run_daily_digest(source=path, date_str="2026-09-14")
                self.assertEqual(mock_post.call_count, 2)

    def test_no_webhook_configured_is_attempted_true_delivered_false(self):
        with tempfile.TemporaryDirectory() as board_dir:
            path = os.path.join(board_dir, "board.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"sport": "soccer", "generated_at": datetime.now(timezone.utc).isoformat(), "props": []}, f)
            with patch("soccer_adapter.score_dabble_soccer_board", return_value=[_fake_candidate("Player A")]), \
                 patch.dict(os.environ, {}, clear=False):
                os.environ.pop(digest.WEBHOOK_ENV_VAR, None)
                os.environ.pop(digest.FALLBACK_WEBHOOK_ENV_VAR, None)
                record = digest.run_daily_digest(source=path, date_str="2026-09-14")
                self.assertFalse(record["discord_attempted"], "no webhook configured is a documented no-op")
                self.assertFalse(record["discord_delivered"])


class TestDigestSelection(unittest.TestCase):
    def test_shows_at_least_15_and_every_watch_tier_candidate(self):
        candidates = [_fake_candidate(f"Player {i}", dns=10) for i in range(20)]
        # dns=75 is the ALERT band's floor ([75,85), see soccer_dashboard.
        # tier_counts, item 5) - watch is [70,75).
        candidates += [_fake_candidate(f"Watch {i}", dns=75) for i in range(3)]
        shown, watch_count, alert_count, high_count = digest.select_digest_candidates(candidates)
        self.assertEqual(watch_count, 0)
        self.assertEqual(alert_count, 3)
        self.assertGreaterEqual(len(shown), digest.MIN_CANDIDATES_SHOWN)
        self.assertTrue(all(c["dns_score"] == 75 for c in shown if c["player_name"].startswith("Watch")))
        self.assertEqual(sum(1 for c in shown if c["dns_score"] == 75), 3, "every Watch+ candidate must be shown")

    def test_watch_tier_overflow_beyond_15_still_all_shown(self):
        candidates = [_fake_candidate(f"Watch {i}", dns=90) for i in range(20)]
        shown, watch_count, alert_count, high_count = digest.select_digest_candidates(candidates)
        self.assertEqual(watch_count, 0)
        self.assertEqual(alert_count, 0)
        self.assertEqual(high_count, 20)
        self.assertEqual(len(shown), 20, "all 20 Watch+ candidates must show even though that's > 15")

    def test_tier_counts_agree_with_the_dashboard_exactly(self):
        """item 5, 2026-09-14: found live that the digest reported "7
        WATCH" (its own cumulative dns_score>=70 count) while docs/
        soccer-dns.html's chip showed "0 Watch" (an exclusive [70,75)
        band) for the SAME board - both "correct" for a different
        definition of the word. Both must now come from the exact same
        function, so they can never disagree again."""
        import soccer_dashboard
        candidates = ([_fake_candidate("A", dns=72)] + [_fake_candidate("B", dns=78)] +
                      [_fake_candidate("C", dns=90)])
        _shown, watch_count, alert_count, high_count = digest.select_digest_candidates(candidates)
        _live, dash_watch, dash_alert, dash_high, _critical = soccer_dashboard.tier_counts(candidates)
        self.assertEqual((watch_count, alert_count, high_count), (dash_watch, dash_alert, dash_high))
        self.assertEqual((watch_count, alert_count, high_count), (1, 1, 1))


class TestRealTimeAlertsIndependentOfDigestDedupe(_TempDigestDir):
    def test_digest_delivery_never_touches_soccer_alerts_state(self):
        """The two modules must never share a dedup store - a digest send
        for today must not mark/alter anything soccer_alerts.py reads."""
        import soccer_alerts
        with tempfile.TemporaryDirectory() as alerts_dir, tempfile.TemporaryDirectory() as board_dir:
            path = os.path.join(board_dir, "board.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"sport": "soccer", "generated_at": datetime.now(timezone.utc).isoformat(), "props": []}, f)
            with patch.object(soccer_alerts, "ALERTS_DIR", alerts_dir), \
                 patch("soccer_adapter.score_dabble_soccer_board", return_value=[_fake_candidate("Player A", dns=90)]), \
                 patch.object(digest, "_post_discord", return_value=(True, True, 204, None)):
                digest.run_daily_digest(source=path, date_str="2026-09-14")
                self.assertEqual(os.listdir(alerts_dir), [], "digest delivery must not write to soccer_alerts' store")

            # And the reverse: soccer_alerts firing must not touch the digest dir.
            self.assertFalse(os.path.exists(digest._digest_path("2026-09-15")))
            with patch.object(soccer_alerts, "ALERTS_DIR", alerts_dir), \
                 patch.object(soccer_alerts, "send_discord_alert", return_value=True):
                soccer_alerts.notify_soccer_candidates(
                    [_fake_candidate("Player A", dns=90)], date_str="2026-09-15")
            self.assertFalse(os.path.exists(digest._digest_path("2026-09-15")),
                              "a real-time alert must never create/alter a digest record")


class TestIsDueNow(unittest.TestCase):
    """LA-anchored gate for the dense-cron production schedule (item 1) -
    a GitHub Actions cron is UTC-only and DST-blind, so the workflow
    fires twice (covering both PDT/PST UTC offsets) and this decides
    which of the two firings is actually "7am Pacific" right now."""

    def test_due_during_pdt_target_hour(self):
        # 14:30 UTC = 7:30 AM PDT (UTC-7) in September.
        now = datetime(2026, 9, 14, 14, 30, tzinfo=timezone.utc)
        self.assertTrue(digest.is_due_now(target_hour=7, now=now))

    def test_not_due_one_hour_later_under_the_old_narrow_window(self):
        # 15:30 UTC = 8:30 AM PDT - outside a 1h window, still explicitly
        # supported via window_hours for anything that wants the old
        # narrow behavior.
        now = datetime(2026, 9, 14, 15, 30, tzinfo=timezone.utc)
        self.assertFalse(digest.is_due_now(target_hour=7, window_hours=1, now=now))

    def test_due_during_pst_target_hour(self):
        # 15:30 UTC = 7:30 AM PST (UTC-8) in January.
        now = datetime(2026, 1, 14, 15, 30, tzinfo=timezone.utc)
        self.assertTrue(digest.is_due_now(target_hour=7, now=now))

    def test_not_due_far_from_target(self):
        now = datetime(2026, 9, 14, 2, 0, tzinfo=timezone.utc)
        self.assertFalse(digest.is_due_now(target_hour=7, now=now))

    def test_widened_default_window_tolerates_a_multi_hour_scheduling_delay(self):
        """2026-09-16: confirmed live via `gh run list` that both of a
        day's scheduled fires landed 3-4h late, missing the old 1-hour
        window entirely. The new 6h default must still see a fire that
        lands hours late as due."""
        # 17:51 UTC = 10:51 AM PDT - the exact real delayed-fire time observed live.
        now = datetime(2026, 9, 15, 17, 51, tzinfo=timezone.utc)
        self.assertTrue(digest.is_due_now(target_hour=7, now=now))

    def test_still_not_due_well_outside_the_widened_window(self):
        # 22:00 UTC = 3:00 PM PDT - past even the widened 6h window (7am-1pm).
        now = datetime(2026, 9, 15, 22, 0, tzinfo=timezone.utc)
        self.assertFalse(digest.is_due_now(target_hour=7, now=now))


class TestHeartbeat(unittest.TestCase):
    """item 4, 2026-09-16: silence should mean "nothing crossed a
    threshold," not "is this even running." No real network - _post_
    discord is always mocked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch_alerts_dir = patch("soccer_alerts.ALERTS_DIR", self._tmp.name)
        self._patch_alerts_dir.start()
        self._patch_post = patch("soccer_daily_digest._post_discord", return_value=(True, True, 204, None))
        self.mock_post = self._patch_post.start()

    def tearDown(self):
        self._patch_post.stop()
        self._patch_alerts_dir.stop()
        self._tmp.cleanup()

    def _candidate_with_kickoff(self, hours_from_now, now, dns=80):
        c = _fake_candidate("Player A", dns=dns)
        c["event_date"] = (now + timedelta(hours=hours_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return c

    def test_no_upcoming_fixtures_sends_nothing(self):
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        candidates = [self._candidate_with_kickoff(-1, now)]
        sent = digest.maybe_send_heartbeat(candidates, "2026-09-16", now=now)
        self.assertFalse(sent)
        self.mock_post.assert_not_called()

    def test_recent_real_alert_suppresses_the_heartbeat(self):
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        import soccer_alerts
        soccer_alerts._save_alerts("2026-09-16", {
            "rec1": {"alerted_at": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        })
        candidates = [self._candidate_with_kickoff(3, now)]
        sent = digest.maybe_send_heartbeat(candidates, "2026-09-16", now=now)
        self.assertFalse(sent)
        self.mock_post.assert_not_called()

    def test_stale_alert_and_upcoming_fixture_sends_a_heartbeat(self):
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        import soccer_alerts
        soccer_alerts._save_alerts("2026-09-16", {
            "rec1": {"alerted_at": (now - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        })
        candidates = [self._candidate_with_kickoff(3, now)]
        sent = digest.maybe_send_heartbeat(candidates, "2026-09-16", now=now)
        self.assertTrue(sent)
        self.mock_post.assert_called_once()
        message = self.mock_post.call_args.kwargs.get("content") or self.mock_post.call_args[0][0]
        self.assertIn("DNS running", message)

    def test_never_alerted_today_and_upcoming_fixture_sends_a_heartbeat(self):
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        candidates = [self._candidate_with_kickoff(3, now)]
        sent = digest.maybe_send_heartbeat(candidates, "2026-09-16", now=now)
        self.assertTrue(sent)

    def test_heartbeat_source_is_tagged_for_discord_health(self):
        now = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
        candidates = [self._candidate_with_kickoff(3, now)]
        digest.maybe_send_heartbeat(candidates, "2026-09-16", now=now)
        self.assertEqual(self.mock_post.call_args.kwargs.get("source"), "heartbeat")


class TestRunIntradayCheck(unittest.TestCase):
    """item 2, 2026-09-16: the intraday scheduler's orchestration - most
    fires must do nothing beyond a couple of fast local checks, and a
    real scoring pass must only happen when soccer_scheduler says
    something is actually due."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch_state = patch("soccer_scheduler.STATE_PATH", os.path.join(self._tmp.name, "state.json"))
        self._patch_state.start()
        self._patch_heartbeat = patch("soccer_daily_digest.maybe_send_heartbeat", return_value=False)
        self._patch_heartbeat.start()
        self._patch_dashboard = patch("soccer_dashboard.generate")
        self._patch_dashboard.start()

    def tearDown(self):
        self._patch_dashboard.stop()
        self._patch_heartbeat.stop()
        self._patch_state.stop()
        self._tmp.cleanup()

    def test_nothing_due_does_not_score_or_refresh(self):
        with patch("soccer_scheduler.current_fixtures", return_value=[]), \
             patch("soccer_scheduler.board_fetch_due", return_value=(False, "not_due")), \
             patch("soccer_scheduler.any_fixture_due", return_value=False) as mock_any_due, \
             patch("soccer_adapter.score_dabble_soccer_board") as mock_score, \
             patch("soccer_daily_digest._attempt_board_refresh") as mock_refresh:
            result = digest.run_intraday_check()
        mock_score.assert_not_called()
        mock_refresh.assert_not_called()
        self.assertFalse(result["scored"])

    def test_fixture_due_triggers_a_real_scoring_pass_with_notify_true(self):
        with patch("soccer_scheduler.current_fixtures", return_value=[{"fixture_id": "f1", "kickoff": None}]), \
             patch("soccer_scheduler.board_fetch_due", return_value=(False, "not_due")), \
             patch("soccer_scheduler.any_fixture_due", return_value=True), \
             patch("soccer_adapter.score_dabble_soccer_board", return_value=[_fake_candidate("A")]) as mock_score, \
             patch("soccer_daily_digest._attempt_board_refresh") as mock_refresh:
            result = digest.run_intraday_check()
        mock_score.assert_called_once()
        self.assertTrue(mock_score.call_args.kwargs.get("notify"))
        mock_refresh.assert_not_called()
        self.assertTrue(result["scored"])
        self.assertEqual(result["candidate_count"], 1)

    def test_board_due_triggers_a_refresh_before_the_due_check(self):
        with patch("soccer_scheduler.current_fixtures", return_value=[]), \
             patch("soccer_scheduler.board_fetch_due", return_value=(True, "interval_elapsed")), \
             patch("soccer_scheduler.any_fixture_due", return_value=False), \
             patch("soccer_daily_digest._attempt_board_refresh", return_value=True) as mock_refresh, \
             patch("soccer_adapter.score_dabble_soccer_board") as mock_score:
            result = digest.run_intraday_check()
        mock_refresh.assert_called_once()
        self.assertTrue(result["board_refreshed"])

    def test_state_is_updated_after_a_real_scoring_pass(self):
        with patch("soccer_scheduler.current_fixtures", return_value=[]), \
             patch("soccer_scheduler.board_fetch_due", return_value=(False, "not_due")), \
             patch("soccer_scheduler.any_fixture_due", return_value=True), \
             patch("soccer_adapter.score_dabble_soccer_board", return_value=[]), \
             patch("soccer_daily_digest._attempt_board_refresh"):
            digest.run_intraday_check()
        state = __import__("soccer_scheduler").load_state()
        self.assertIsNotNone(state["last_checked_at"])


if __name__ == "__main__":
    unittest.main()
