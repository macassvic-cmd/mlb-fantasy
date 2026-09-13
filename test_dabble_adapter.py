"""Unit tests for dabble_adapter.py. Run with: python -m unittest test_dabble_adapter -v

All MLB API calls (get_games, get_team_abbreviations, get_active_roster,
get_game_start_data) are mocked - no live network access. _find_game_for_team
and _build_lineup_index (imported from stale_lines.py) are pure functions
over the fake `games` list built below, so they run for real rather than
being mocked - that's the actual game/lineup-resolution logic under test.
"""

import unittest
from unittest.mock import patch

import dabble_adapter

TEAM_ABBREVS = {"NYY": 147, "BOS": 111, "LAD": 119, "SF": 137}

GAME_NOT_YET_POSTED = {
    "gamePk": 900001,
    "gameDate": "2026-09-13T23:05:00Z",
    "teams": {"home": {"team": {"id": 111}}, "away": {"team": {"id": 147}}},
    "lineups": {"homePlayers": [], "awayPlayers": []},
}

GAME_POSTED_PLAYER_BENCHED = {
    "gamePk": 900002,
    "gameDate": "2026-09-13T23:05:00Z",
    "teams": {"home": {"team": {"id": 119}}, "away": {"team": {"id": 137}}},
    "lineups": {
        "homePlayers": [{"fullName": "Someone Else"}],  # posted, but our test player isn't in it
        "awayPlayers": [{"fullName": "Away Starter"}],
    },
}


def _fake_get_games(date_str):
    return [GAME_NOT_YET_POSTED, GAME_POSTED_PLAYER_BENCHED]


def _fake_get_active_roster(team_id):
    return {12345: "Test Player", 67890: "Jose Ramirez"}


def _fake_get_game_start_data(game_pk):
    return {
        "home": {"opp_pitcher_id": 1, "opp_pitcher_hand": "R", "players": []},
        "away": {"opp_pitcher_id": 2, "opp_pitcher_hand": "L", "players": []},
    }


def _patch_mlb_api():
    return (
        patch("dabble_adapter.get_games", side_effect=_fake_get_games),
        patch("dabble_adapter.get_team_abbreviations", return_value=TEAM_ABBREVS),
        patch("dabble_adapter.get_active_roster", side_effect=_fake_get_active_roster),
        patch("dabble_adapter.get_game_start_data", side_effect=_fake_get_game_start_data),
    )


class TestPlayerAbsentFromProjectedLineup(unittest.TestCase):
    """A player Dabble has a live prop on, whose team's lineup hasn't
    posted yet (so pipeline.py's own projection either doesn't have them
    or is a pure guess) must still be scored - that's the entire point of
    Phase 2."""

    def test_unconfirmed_player_gets_scored(self):
        board = {"sport": "mlb", "generated_at": "2026-09-13T10:00:00Z", "props": [
            {"player_name": "Test Player", "team": "NYY", "market": "Hits", "line": 0.5,
             "event_date": "2026-09-13T23:05:00Z", "matchup": "NYY @ BOS", "position": "SS"},
        ]}
        patches = _patch_mlb_api()
        with patches[0], patches[1], patches[2], patches[3]:
            candidates = dabble_adapter.score_dabble_board(source=board, persist=False)
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c["player_name"], "Test Player")
        self.assertIsInstance(c["score"], int)
        self.assertEqual(c["projected_start_status"], "not_yet_posted")


class TestDuplicatePropsNoDuplicateScoring(unittest.TestCase):
    def test_exact_duplicate_prop_collapses(self):
        raw = {"player_name": "Test Player", "team": "NYY", "market": "Hits", "line": 0.5,
               "event_date": "2026-09-13T23:05:00Z", "matchup": "NYY @ BOS"}
        props = dabble_adapter.load_dabble_props({"sport": "mlb", "props": [raw, dict(raw)]})
        self.assertEqual(len(props), 1, "an exact-duplicate prop record must collapse to one")

    def test_multiple_markets_same_player_scores_once(self):
        board = {"sport": "mlb", "generated_at": "2026-09-13T10:00:00Z", "props": [
            {"player_name": "Test Player", "team": "NYY", "market": "Hits", "line": 0.5,
             "event_date": "2026-09-13T23:05:00Z", "matchup": "NYY @ BOS", "position": "SS"},
            {"player_name": "Test Player", "team": "NYY", "market": "Total Bases", "line": 1.5,
             "event_date": "2026-09-13T23:05:00Z", "matchup": "NYY @ BOS", "position": "SS"},
            {"player_name": "Test Player", "team": "NYY", "market": "Runs", "line": 0.5,
             "event_date": "2026-09-13T23:05:00Z", "matchup": "NYY @ BOS", "position": "SS"},
        ]}
        patches = _patch_mlb_api()
        with patches[0], patches[1], patches[2], patches[3]:
            candidates = dabble_adapter.score_dabble_board(source=board, persist=False)
        self.assertEqual(len(candidates), 1, "three markets for one player must produce ONE scored candidate")
        self.assertEqual(len(candidates[0]["props"]), 3, "but all three props should still be attached to it")


class TestNameNormalization(unittest.TestCase):
    def test_accented_and_plain_name_group_together(self):
        board = {"sport": "mlb", "props": [
            {"player_name": "José Ramírez", "team": "LAD", "market": "Hits", "line": 0.5,
             "event_date": "2026-09-13T23:05:00Z", "matchup": "SF @ LAD", "position": "3B"},
            {"player_name": "Jose Ramirez", "team": "LAD", "market": "Runs", "line": 0.5,
             "event_date": "2026-09-13T23:05:00Z", "matchup": "SF @ LAD", "position": "3B"},
        ]}
        props = dabble_adapter.load_dabble_props(board)
        players = dabble_adapter.group_props_by_player(props)
        self.assertEqual(len(players), 1, "accented vs. plain spelling of the same name must group together")
        (_, entry), = players.items()
        self.assertEqual(len(entry["props"]), 2)

    def test_suffix_variant_groups_together(self):
        board = {"sport": "mlb", "props": [
            {"player_name": "Bobby Witt Jr.", "team": "KC", "market": "Hits", "line": 0.5,
             "event_date": "2026-09-13T23:05:00Z", "matchup": "KC @ BOS", "position": "SS"},
            {"player_name": "Bobby Witt", "team": "KC", "market": "Runs", "line": 0.5,
             "event_date": "2026-09-13T23:05:00Z", "matchup": "KC @ BOS", "position": "SS"},
        ]}
        props = dabble_adapter.load_dabble_props(board)
        players = dabble_adapter.group_props_by_player(props)
        self.assertEqual(len(players), 1, "'Jr.' suffix variants must normalize to the same player")


class TestGracefulEnrichmentFailure(unittest.TestCase):
    """A single failing enrichment source (roster lookup, opposing-pitcher
    lookup) must degrade that one signal, never block scoring entirely -
    a few hundred players are scored in one pass and one bad lookup can't
    be allowed to sink the rest."""

    def test_scoring_continues_when_roster_lookup_fails(self):
        board = {"sport": "mlb", "generated_at": "2026-09-13T10:00:00Z", "props": [
            {"player_name": "Test Player", "team": "NYY", "market": "Hits", "line": 0.5,
             "event_date": "2026-09-13T23:05:00Z", "matchup": "NYY @ BOS", "position": "SS"},
        ]}
        with patch("dabble_adapter.get_games", side_effect=_fake_get_games), \
             patch("dabble_adapter.get_team_abbreviations", return_value=TEAM_ABBREVS), \
             patch("dabble_adapter.get_active_roster", side_effect=RuntimeError("roster API down")), \
             patch("dabble_adapter.get_game_start_data", side_effect=_fake_get_game_start_data):
            candidates = dabble_adapter.score_dabble_board(source=board, persist=False)
        self.assertEqual(len(candidates), 1)
        self.assertIsNone(candidates[0]["player_id"], "no roster match possible, but scoring must still complete")
        self.assertIsInstance(candidates[0]["score"], int)

    def test_scoring_continues_when_pitcher_lookup_fails(self):
        board = {"sport": "mlb", "generated_at": "2026-09-13T10:00:00Z", "props": [
            {"player_name": "Test Player", "team": "NYY", "market": "Hits", "line": 0.5,
             "event_date": "2026-09-13T23:05:00Z", "matchup": "NYY @ BOS", "position": "SS"},
        ]}
        with patch("dabble_adapter.get_games", side_effect=_fake_get_games), \
             patch("dabble_adapter.get_team_abbreviations", return_value=TEAM_ABBREVS), \
             patch("dabble_adapter.get_active_roster", side_effect=_fake_get_active_roster), \
             patch("dabble_adapter.get_game_start_data", side_effect=RuntimeError("live feed down")):
            candidates = dabble_adapter.score_dabble_board(source=board, persist=False)
        self.assertEqual(len(candidates), 1)
        self.assertIsNone(candidates[0]["opposing_pitcher_hand"])
        self.assertIsInstance(candidates[0]["score"], int)


class TestLiveSnapshotGrading(unittest.TestCase):
    def test_snapshot_can_be_graded_against_actual_outcome(self):
        candidates = [{
            "player_name": "Test Player", "normalized_name": "test player", "team": "NYY",
            "player_id": 12345, "game_id": 900001, "score": 82, "projected_start_status": "not_yet_posted",
        }]
        with patch("dabble_adapter.LIVE_SNAPSHOTS_DIR", "data/_test_dnp_live_snapshots"):
            dabble_adapter.record_live_snapshot("2099-01-01", candidates)

            def fake_game_data(game_pk):
                return {
                    "home": {"players": []},
                    "away": {"players": [{"player_id": 12345, "started": False}]},
                }

            with patch("dabble_adapter.get_game_start_data", side_effect=fake_game_data):
                graded = dabble_adapter.grade_live_snapshot("2099-01-01")

            import os
            os.remove("data/_test_dnp_live_snapshots/2099-01-01.json")

        self.assertEqual(len(graded), 1)
        self.assertEqual(graded[0]["actual_started"], False)
        self.assertEqual(graded[0]["final_score"], 82)


if __name__ == "__main__":
    unittest.main()
