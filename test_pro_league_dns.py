"""Unit tests for pro_league_dns.py. Run with:
python -m unittest test_pro_league_dns -v

All tests mock scrapers.espn_pro_leagues and Discord posting - no live
network calls, no writes to the real data/pro_league_dns/ state."""

import shutil
import tempfile
import unittest
from unittest.mock import patch

import pro_league_dns as pld


class TestRunScan(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.mkdtemp()
        self._orig_template = pld.STATE_DIR_TEMPLATE
        pld.STATE_DIR_TEMPLATE = self._tmp_dir + "/{league}"
        self._discord_post_patch = patch("pro_league_dns._discord_post", return_value=False)
        self._send_digest_patch = patch("pro_league_dns.send_digest", return_value=0)
        self._discord_post_patch.start()
        self._send_digest_patch.start()

    def tearDown(self):
        pld.STATE_DIR_TEMPLATE = self._orig_template
        self._discord_post_patch.stop()
        self._send_digest_patch.stop()
        shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _entry(self, **overrides):
        e = {
            "name": "Player One", "normalized_name": "player one", "team": "Kansas City Chiefs",
            "event_date_utc": "2026-09-10T20:00:00Z", "markets": {"PASSING_YARDS": 250.5},
        }
        e.update(overrides)
        return e

    def test_confirmed_out_no_return_date_flags(self):
        with patch("scrapers.espn_pro_leagues.match_espn_team_id", return_value="12"), \
             patch("scrapers.espn_pro_leagues.get_team_injuries_cached", return_value=[
                 {"name": "Player One", "normalized_name": "player one", "status": "Out",
                  "since": "2026-09-01", "expected_return_date": None},
             ]):
            summary = pld.run_scan([self._entry()], league="nfl")
        self.assertEqual(summary["counters"]["new_flags"], 1)
        self.assertEqual(summary["active_unresolved_flags"], 1)

    def test_return_date_after_fixture_flags(self):
        with patch("scrapers.espn_pro_leagues.match_espn_team_id", return_value="12"), \
             patch("scrapers.espn_pro_leagues.get_team_injuries_cached", return_value=[
                 {"name": "Player One", "normalized_name": "player one", "status": "Injured Reserve",
                  "since": "2026-09-01", "expected_return_date": "2026-12-01"},
             ]):
            summary = pld.run_scan([self._entry()], league="nfl")
        self.assertEqual(summary["counters"]["new_flags"], 1)

    def test_return_date_before_fixture_does_not_flag(self):
        with patch("scrapers.espn_pro_leagues.match_espn_team_id", return_value="12"), \
             patch("scrapers.espn_pro_leagues.get_team_injuries_cached", return_value=[
                 {"name": "Player One", "normalized_name": "player one", "status": "Out",
                  "since": "2026-09-01", "expected_return_date": "2026-09-05"},
             ]):
            summary = pld.run_scan([self._entry()], league="nfl")
        self.assertEqual(summary["counters"]["new_flags"], 0)

    def test_player_not_in_injury_list_does_not_flag(self):
        with patch("scrapers.espn_pro_leagues.match_espn_team_id", return_value="12"), \
             patch("scrapers.espn_pro_leagues.get_team_injuries_cached", return_value=[]):
            summary = pld.run_scan([self._entry()], league="nfl")
        self.assertEqual(summary["counters"]["new_flags"], 0)

    def test_unmatched_team_counted_not_flagged(self):
        with patch("scrapers.espn_pro_leagues.match_espn_team_id", return_value=None):
            summary = pld.run_scan([self._entry()], league="nfl")
        self.assertEqual(summary["counters"]["no_team_match"], 1)
        self.assertEqual(summary["counters"]["new_flags"], 0)

    def test_already_flagged_on_second_scan(self):
        with patch("scrapers.espn_pro_leagues.match_espn_team_id", return_value="12"), \
             patch("scrapers.espn_pro_leagues.get_team_injuries_cached", return_value=[
                 {"name": "Player One", "normalized_name": "player one", "status": "Out",
                  "since": "2026-09-01", "expected_return_date": None},
             ]):
            first = pld.run_scan([self._entry()], league="nfl")
            second = pld.run_scan([self._entry()], league="nfl")
        self.assertEqual(first["counters"]["new_flags"], 1)
        self.assertEqual(second["counters"]["new_flags"], 0)
        self.assertEqual(second["counters"]["already_flagged"], 1)
        self.assertEqual(second["total_tracked_flags"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
