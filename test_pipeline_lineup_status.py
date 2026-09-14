"""Regression tests for the 2026-09-14 lineup-confirmation bug fix:

  - a stale/failed fetch must never downgrade a player who was already
    confirmed earlier the same day (pipeline._protect_against_lineup_
    regression)
  - projected -> confirmed upgrades correctly once MLB posts a lineup
  - lineup_status/lineup_source/lineup_confirmed survive report.py's
    row-building + JSON serialization
  - date/timezone rollover: "today" must be the Pacific baseball day,
    never a bare UTC date (scrapers.mlb_api.mlb_today_str)

Isolation: the one test that needs a "previous day's file on disk" uses
date "2099-01-01" (never a real slate) under data/, written and removed
within the test itself - no real data/{date}.json is ever touched.
"""

import json
import os
import unittest
from datetime import datetime, timezone

import pipeline
import report
from scrapers.mlb_api import mlb_today_str

FAKE_DATE = "2099-01-01"
FAKE_DATA_PATH = os.path.join("data", f"{FAKE_DATE}.json")


class TestMlbTodayTimezoneRollover(unittest.TestCase):
    """The core UTC-vs-Pacific bug: a server clocked in UTC computes a
    calendar date up to 7-8 hours ahead of Pacific's actual "today" -
    mlb_today_str must always resolve to the Pacific day instead."""

    def test_utc_evening_pacific_still_same_day(self):
        # 2026-09-15 02:00 UTC = 2026-09-14 19:00 PDT (UTC-7) - a bare
        # UTC date here would wrongly say "2026-09-15".
        now = datetime(2026, 9, 15, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(mlb_today_str(now), "2026-09-14")

    def test_utc_morning_matches_pacific_previous_evening(self):
        # 2026-09-14 06:00 UTC = 2026-09-13 23:00 PDT - still "yesterday"
        # in Pacific even though the UTC calendar has already turned over.
        now = datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc)
        self.assertEqual(mlb_today_str(now), "2026-09-13")

    def test_utc_midday_matches_pacific_same_day(self):
        # No rollover risk mid-morning UTC - both agree.
        now = datetime(2026, 9, 14, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(mlb_today_str(now), "2026-09-14")

    def test_naive_datetime_assumed_utc(self):
        now = datetime(2026, 9, 15, 2, 0)  # naive, no tzinfo
        self.assertEqual(mlb_today_str(now), "2026-09-14")


class TestFillMissingLineupsStamping(unittest.TestCase):
    def test_confirmed_lineup_stamped_with_source_fields(self):
        mlb_lineups = {
            1: {"game_pk": 1, "team_id": 10, "lineup_status": "confirmed",
                "lineup_source": "mlb_official", "lineup_source_updated_at": "2026-09-14T20:00:00+00:00",
                "lineup_fetched_at": "2026-09-14T20:00:00+00:00"},
        }
        games = [{"gamePk": 1, "teams": {
            "home": {"team": {"id": 10, "name": "Home"}, "probablePitcher": {}},
            "away": {"team": {"id": 20, "name": "Away"}, "probablePitcher": {}}},
            "venue": {}, "gameDate": "2026-09-14T23:00:00Z"}]
        from unittest.mock import patch
        with patch("scrapers.mlb_api.get_recent_team_lineup", return_value=[]):
            result = pipeline._fill_missing_lineups(dict(mlb_lineups), games, "2026-09-14")
        self.assertEqual(result[1]["lineup_status"], "confirmed")
        self.assertEqual(result[1]["lineup_source"], "mlb_official")

    def test_missing_team_falls_back_to_projected_with_its_own_source(self):
        mlb_lineups = {}  # neither side confirmed
        games = [{"gamePk": 1, "teams": {
            "home": {"team": {"id": 10, "name": "Home"}, "probablePitcher": {}},
            "away": {"team": {"id": 20, "name": "Away"}, "probablePitcher": {}}},
            "venue": {}, "gameDate": "2026-09-14T23:00:00Z"}]
        from unittest.mock import patch
        with patch("scrapers.mlb_api.get_recent_team_lineup", return_value=[101, 102]):
            result = pipeline._fill_missing_lineups(mlb_lineups, games, "2026-09-14")
        self.assertEqual(result[101]["lineup_status"], "projected")
        self.assertEqual(result[101]["lineup_source"], "mlb_recent_game_projection")
        self.assertIsNone(result[101]["lineup_source_updated_at"])


class TestLineupRegressionGuard(unittest.TestCase):
    """A stale/failed same-day fetch must never downgrade a previously-
    confirmed player - see pipeline._protect_against_lineup_regression."""

    def setUp(self):
        os.makedirs("data", exist_ok=True)
        self._prior = [
            {"player_id": 501, "name": "Confirmed Starter", "team_name": "Dodgers", "game_pk": 900,
             "lineup_status": "confirmed", "lineup_source": "mlb_official",
             "lineup_source_updated_at": "2026-09-14T18:00:00+00:00"},
            {"player_id": 502, "name": "Was Projected", "team_name": "Reds", "game_pk": 900,
             "lineup_status": "projected", "lineup_source": "mlb_recent_game_projection",
             "lineup_source_updated_at": None},
        ]
        with open(FAKE_DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(self._prior, f)

    def tearDown(self):
        if os.path.exists(FAKE_DATA_PATH):
            os.remove(FAKE_DATA_PATH)

    def test_stale_empty_fetch_cannot_overwrite_confirmed(self):
        """This run's fetch came back with NOTHING for player 501's game
        (transient API gap) - the prior confirmed record must be
        retained, not silently dropped/downgraded."""
        mlb_lineups, carried_over = pipeline._protect_against_lineup_regression("2099-01-01", {})
        self.assertEqual(len(carried_over), 1)
        self.assertEqual(carried_over[0]["player_id"], 501)
        self.assertEqual(carried_over[0]["lineup_status"], "confirmed")
        self.assertIn("retained", carried_over[0]["lineup_source"])
        # player 502 was only ever projected - nothing to protect.
        self.assertNotIn(502, {r.get("player_id") for r in carried_over})

    def test_upgrade_to_confirmed_this_run_is_not_treated_as_regression(self):
        """The normal, expected case: MLB has now posted the lineup this
        run - the fresh confirmed read must be used as-is, not overridden
        by anything from the guard."""
        mlb_lineups = {501: {"game_pk": 900, "lineup_status": "confirmed",
                              "lineup_source": "mlb_official",
                              "lineup_source_updated_at": "2026-09-14T21:00:00+00:00"}}
        result, carried_over = pipeline._protect_against_lineup_regression("2099-01-01", dict(mlb_lineups))
        self.assertEqual(carried_over, [])
        self.assertEqual(result[501]["lineup_source_updated_at"], "2026-09-14T21:00:00+00:00")

    def test_new_run_still_confirmed_but_downgraded_status_is_guarded(self):
        """Even if the player still appears this run, a fetch anomaly
        that marks a previously-confirmed player as merely "projected"
        this cycle must be guarded, not trusted at face value."""
        mlb_lineups = {501: {"game_pk": 900, "lineup_status": "projected",
                              "lineup_source": "mlb_recent_game_projection"}}
        result, carried_over = pipeline._protect_against_lineup_regression("2099-01-01", dict(mlb_lineups))
        self.assertEqual(len(carried_over), 1)
        self.assertEqual(carried_over[0]["player_id"], 501)
        self.assertNotIn(501, result, "the guarded player must be pulled from mlb_lineups, not double-processed")

    def test_no_prior_file_is_a_safe_noop(self):
        result, carried_over = pipeline._protect_against_lineup_regression("2098-01-01", {1: {"lineup_status": "confirmed"}})
        self.assertEqual(carried_over, [])
        self.assertIn(1, result)


class TestReportSerializationPreservesLineupStatus(unittest.TestCase):
    """Confirmed lineup status must survive report.build_row + full JSON
    round-trip (dashboard serialization) unchanged."""

    def _player(self, **overrides):
        p = {
            "player_id": 1, "name": "Test Player", "team_name": "Dodgers", "opp_team_name": "Reds",
            "position": "OF", "batting_order": 3, "lineup_status": "confirmed", "lineup_confirmed": True,
            "lineup_source": "mlb_official", "lineup_source_updated_at": "2026-09-14T20:00:00+00:00",
            "lineup_fetched_at": "2026-09-14T20:00:00+00:00", "game_pk": 1, "home_away": "away",
            "venue_name": "GABP", "game_date_utc": "2026-09-14T23:00:00Z",
        }
        p.update(overrides)
        return p

    def test_confirmed_status_round_trips_through_build_row_and_json(self):
        row = report.build_row(self._player())
        self.assertEqual(row["lineup_status"], "confirmed")
        self.assertTrue(row["lineup_confirmed"])
        self.assertEqual(row["lineup_source"], "mlb_official")

        # Full dashboard serialization round-trip (json.dumps -> loads).
        reloaded = json.loads(json.dumps(row))
        self.assertEqual(reloaded["lineup_status"], "confirmed")
        self.assertTrue(reloaded["lineup_confirmed"])

    def test_projected_status_round_trips_and_flags_card_correctly(self):
        row = report.build_row(self._player(lineup_status="projected", lineup_confirmed=False,
                                              lineup_source="mlb_recent_game_projection"))
        self.assertEqual(row["lineup_status"], "projected")
        self.assertFalse(row["lineup_confirmed"])

    def test_confidence_score_rewards_confirmed_lineup(self):
        confirmed = report.confidence_score(self._player())
        projected = report.confidence_score(self._player(lineup_confirmed=False))
        self.assertGreater(confirmed, projected)


class TestConfirmedNonStarterIsAbsentNotDowngraded(unittest.TestCase):
    """Once a team's lineup is confirmed, a player who isn't in it (a
    confirmed non-starter) must not appear at all - not linger as a
    stale "projected starter." _fill_missing_lineups only ever fills a
    TEAM gap, never adds players for a team that already has a confirmed
    lineup, so a benched player is naturally excluded by construction."""

    def test_benched_player_not_added_when_team_already_confirmed(self):
        # BOTH teams already have a confirmed lineup (player 1 for the
        # home side, player 2 for the away side) - a bench player (999,
        # yesterday's starter, since confirmed OUT of today's lineup)
        # must never get added via the projection fallback for either
        # team once that team is already confirmed-covered.
        mlb_lineups = {
            1: {"game_pk": 1, "team_id": 10, "lineup_status": "confirmed", "lineup_source": "mlb_official"},
            2: {"game_pk": 1, "team_id": 20, "lineup_status": "confirmed", "lineup_source": "mlb_official"},
        }
        games = [{"gamePk": 1, "teams": {
            "home": {"team": {"id": 10, "name": "Home"}, "probablePitcher": {}},
            "away": {"team": {"id": 20, "name": "Away"}, "probablePitcher": {}}},
            "venue": {}, "gameDate": "2026-09-14T23:00:00Z"}]
        from unittest.mock import patch
        with patch("scrapers.mlb_api.get_recent_team_lineup", return_value=[999]) as mock_recent:
            result = pipeline._fill_missing_lineups(dict(mlb_lineups), games, "2026-09-14")
            # Both teams are already covered - get_recent_team_lineup
            # (the projection fallback) must never even be consulted.
            mock_recent.assert_not_called()
        self.assertNotIn(999, result)


if __name__ == "__main__":
    unittest.main()
