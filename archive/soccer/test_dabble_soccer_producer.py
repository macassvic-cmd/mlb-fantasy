"""Unit tests for scrapers/dabble_soccer_producer.py - the in-repo port
of the Dabble board fetch (2026-09-14 production-scheduling work, so a
GitHub Actions runner can fetch a fresh board without depending on the
external C:\\platform-tools\\ script on one local machine).

Mocks the HTTP layer entirely - no real network call, no real file
touched (writes go to a tempdir).

Run with: python -m unittest test_dabble_soccer_producer -v
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from scrapers import dabble_soccer_producer as producer


FAKE_COMPETITIONS = [
    {"id": "comp-1", "code": "SOC", "displayName": "EPL", "sportName": "Football"},
    {"id": "comp-2", "code": "SOC", "displayName": "LaLiga", "sportName": "Football"},
    {"id": "comp-3", "code": "MLB", "displayName": "MLB", "sportName": "Baseball"},
]

FAKE_PROP = {
    "id": "prop-1", "playerName": "Amar Dedic", "playerId": "pid-1",
    "teamAbbreviation": "NEW", "marketGroupName": "Assists", "propValue": 0.5,
    "fixtureDate": "2026-09-15T19:00:00.000Z", "fixtureDisplayName": "NEW @ LEE",
    "fixtureId": "fx-1", "playerPosition": "DF",
    "sport": {"name": "Football"}, "competition": {"displayName": "EPL", "name": "EPL"},
}


class TestCanonicalize(unittest.TestCase):
    def test_soccer_prop_maps_sport_correctly(self):
        rec = producer.canonicalize(FAKE_PROP)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["sport"], "soccer")
        self.assertEqual(rec["player_name"], "Amar Dedic")
        self.assertEqual(rec["team"], "NEW")
        self.assertIsNone(rec["dabble_status_raw"])

    def test_missing_required_field_rejected(self):
        bad = dict(FAKE_PROP)
        bad.pop("playerName")
        self.assertIsNone(producer.canonicalize(bad))


class TestGetSoccerCompetitions(unittest.TestCase):
    def test_filters_to_soccer_code_only(self):
        with patch.object(producer, "_get_json", return_value={"data": FAKE_COMPETITIONS}):
            comps = producer.get_soccer_competitions()
        self.assertEqual(len(comps), 2)
        self.assertTrue(all(c["code"] == "SOC" for c in comps))


class TestBuildSoccerBoard(unittest.TestCase):
    def test_combines_every_soccer_competition(self):
        with patch.object(producer, "get_soccer_competitions", return_value=FAKE_COMPETITIONS[:2]), \
             patch.object(producer, "fetch_competition_props", return_value=[FAKE_PROP]):
            board = producer.build_soccer_board()
        self.assertEqual(board["sport"], "soccer")
        self.assertEqual(board["source"], "dabble")
        self.assertEqual(len(board["competitions"]), 2)
        self.assertEqual(len(board["props"]), 2)  # one prop per competition fetched

    def test_one_competition_failing_does_not_abort_the_whole_board(self):
        def fake_fetch(comp):
            if comp["displayName"] == "EPL":
                raise RuntimeError("simulated API failure")
            return [FAKE_PROP]
        with patch.object(producer, "get_soccer_competitions", return_value=FAKE_COMPETITIONS[:2]), \
             patch.object(producer, "fetch_competition_props", side_effect=fake_fetch):
            board = producer.build_soccer_board()
        self.assertEqual(len(board["competitions"]), 1)
        self.assertEqual(board["competitions"][0], "LaLiga")


class TestProduceSoccerBoard(unittest.TestCase):
    def test_writes_board_atomically_to_output_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "board.json")
            with patch.object(producer, "build_soccer_board",
                               return_value={"sport": "soccer", "source": "dabble",
                                             "generated_at": "2026-09-14T12:00:00Z",
                                             "competitions": ["EPL"], "props": [FAKE_PROP]}):
                payload = producer.produce_soccer_board(out_path)
            self.assertTrue(os.path.exists(out_path))
            self.assertFalse(os.path.exists(out_path + ".tmp"), "the .tmp scratch file must not survive")
            with open(out_path, encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk["props"][0]["playerName"], "Amar Dedic")
            self.assertEqual(payload["competitions"], ["EPL"])


if __name__ == "__main__":
    unittest.main()
