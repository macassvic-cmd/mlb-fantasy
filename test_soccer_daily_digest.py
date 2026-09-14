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

    def tearDown(self):
        self._patch_dir.stop()
        self._patch_refresh.stop()
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
        candidates += [_fake_candidate(f"Watch {i}", dns=75) for i in range(3)]
        shown, watch_count, high_count = digest.select_digest_candidates(candidates)
        self.assertEqual(watch_count, 3)
        self.assertGreaterEqual(len(shown), digest.MIN_CANDIDATES_SHOWN)
        self.assertTrue(all(c["dns_score"] == 75 for c in shown if c["player_name"].startswith("Watch")))
        self.assertEqual(sum(1 for c in shown if c["dns_score"] == 75), 3, "every Watch+ candidate must be shown")

    def test_watch_tier_overflow_beyond_15_still_all_shown(self):
        candidates = [_fake_candidate(f"Watch {i}", dns=90) for i in range(20)]
        shown, watch_count, high_count = digest.select_digest_candidates(candidates)
        self.assertEqual(watch_count, 20)
        self.assertEqual(high_count, 20)
        self.assertEqual(len(shown), 20, "all 20 Watch+ candidates must show even though that's > 15")


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


if __name__ == "__main__":
    unittest.main()
