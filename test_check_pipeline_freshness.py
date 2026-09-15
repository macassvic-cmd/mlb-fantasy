"""Unit tests for check_pipeline_freshness.py's item-4 fix (2026-09-15):
a pure elapsed-time gate can't see that a real lineup update happened
since the last fetch. Found live: the morning's dense schedule ends
~11:40am PDT, the day's last real fetch landed ~10:40am PT, and the
afternoon backup fires either skipped (data still <3h old) or got
silently dropped by GitHub's scheduler - the dashboard sat stale through
first pitch with lineups that had since posted.

Uses a fake future date ("2099-01-01") under the real data/ directory -
the same pattern dnp_alerts.py's own tests already use - written and
removed in setUp/tearDown so this never collides with or leaves behind
real production data.

Run with: python -m unittest test_check_pipeline_freshness -v
"""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

import check_pipeline_freshness as cpf

TEST_DATE = "2099-01-01"
TEST_DATA_PATH = os.path.join("data", f"{TEST_DATE}.json")


def _player(game_offset_hours, lineup_confirmed):
    game_time = datetime.now(timezone.utc) + timedelta(hours=game_offset_hours)
    return {
        "player_id": 1, "name": "Test Player",
        "game_date_utc": game_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lineup_confirmed": lineup_confirmed,
    }


class TestAnyUnconfirmedLineupPending(unittest.TestCase):
    def tearDown(self):
        if os.path.exists(TEST_DATA_PATH):
            os.remove(TEST_DATA_PATH)

    def _write(self, players):
        os.makedirs("data", exist_ok=True)
        with open(TEST_DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(players, f)

    def test_no_data_file_returns_false(self):
        self.assertFalse(cpf._any_unconfirmed_lineup_pending(TEST_DATE))

    def test_unconfirmed_lineup_for_future_game_is_pending(self):
        self._write([_player(game_offset_hours=2, lineup_confirmed=False)])
        self.assertTrue(cpf._any_unconfirmed_lineup_pending(TEST_DATE))

    def test_confirmed_lineup_for_future_game_is_not_pending(self):
        self._write([_player(game_offset_hours=2, lineup_confirmed=True)])
        self.assertFalse(cpf._any_unconfirmed_lineup_pending(TEST_DATE))

    def test_unconfirmed_lineup_for_a_game_already_started_is_not_pending(self):
        """Too late for a refresh to matter - the game already started."""
        self._write([_player(game_offset_hours=-1, lineup_confirmed=False)])
        self.assertFalse(cpf._any_unconfirmed_lineup_pending(TEST_DATE))

    def test_one_confirmed_and_one_unconfirmed_upcoming_game_is_pending(self):
        self._write([
            _player(game_offset_hours=1, lineup_confirmed=True),
            _player(game_offset_hours=3, lineup_confirmed=False),
        ])
        self.assertTrue(cpf._any_unconfirmed_lineup_pending(TEST_DATE))

    def test_all_games_started_is_not_pending_even_if_unconfirmed(self):
        self._write([
            _player(game_offset_hours=-1, lineup_confirmed=False),
            _player(game_offset_hours=-2, lineup_confirmed=False),
        ])
        self.assertFalse(cpf._any_unconfirmed_lineup_pending(TEST_DATE))

    def test_missing_game_date_is_skipped_not_a_crash(self):
        self._write([{"player_id": 1, "name": "No Game Time", "lineup_confirmed": False}])
        self.assertFalse(cpf._any_unconfirmed_lineup_pending(TEST_DATE))

    def test_malformed_game_date_is_skipped_not_a_crash(self):
        self._write([{"player_id": 1, "game_date_utc": "not-a-date", "lineup_confirmed": False}])
        self.assertFalse(cpf._any_unconfirmed_lineup_pending(TEST_DATE))

    def test_malformed_json_file_returns_false(self):
        os.makedirs("data", exist_ok=True)
        with open(TEST_DATA_PATH, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        self.assertFalse(cpf._any_unconfirmed_lineup_pending(TEST_DATE))

    def test_lineup_confirmed_defaults_to_false_when_field_missing(self):
        game_time = datetime.now(timezone.utc) + timedelta(hours=2)
        self._write([{"player_id": 1, "game_date_utc": game_time.strftime("%Y-%m-%dT%H:%M:%SZ")}])
        self.assertTrue(cpf._any_unconfirmed_lineup_pending(TEST_DATE))


if __name__ == "__main__":
    unittest.main()
