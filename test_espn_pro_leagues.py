"""Unit tests for scrapers/espn_pro_leagues.py. Run with:
python -m unittest test_espn_pro_leagues -v

Mocks the ESPN HTTP layer (_get) - no live network calls."""

import unittest
from unittest.mock import patch

from scrapers import espn_pro_leagues as epl

FAKE_NFL_TEAMS = {
    "sports": [{"leagues": [{"teams": [
        {"team": {"id": "1", "abbreviation": "ATL", "displayName": "Atlanta Falcons"}},
        {"team": {"id": "22", "abbreviation": "ARI", "displayName": "Arizona Cardinals"}},
        {"team": {"id": "30", "abbreviation": "WSH", "displayName": "Washington Commanders"}},
    ]}]}]
}


class TestMatchEspnTeamId(unittest.TestCase):
    def setUp(self):
        epl._team_cache.clear()
        epl._abbrev_cache.clear()

    def test_exact_abbreviation_match(self):
        with patch.object(epl, "_get", return_value=FAKE_NFL_TEAMS):
            self.assertEqual(epl.match_espn_team_id("nfl", "ATL"), "1")

    def test_abbreviation_alias_mismatch(self):
        # Book says "WAS", ESPN's own abbreviation is "WSH" - found live
        # 2026-09-09 comparing a real export's codes against ESPN's.
        with patch.object(epl, "_get", return_value=FAKE_NFL_TEAMS):
            self.assertEqual(epl.match_espn_team_id("nfl", "WAS"), "30")

    def test_full_name_fuzzy_fallback(self):
        with patch.object(epl, "_get", return_value=FAKE_NFL_TEAMS):
            self.assertEqual(epl.match_espn_team_id("nfl", "Atlanta Falcons"), "1")

    def test_unknown_team_returns_none(self):
        with patch.object(epl, "_get", return_value=FAKE_NFL_TEAMS):
            self.assertIsNone(epl.match_espn_team_id("nfl", "Nonexistent Team"))


class TestFetchTeamInjuries(unittest.TestCase):
    def _injury_page(self, refs):
        return {"items": [{"$ref": r} for r in refs], "pageCount": 1}

    def test_fantasy_status_used_when_top_level_status_is_numeric_glitch(self):
        # Found live 2026-09-09: some ESPN records have a bare numeric
        # top-level "status" ("7") while details.fantasyStatus.
        # abbreviation correctly says "OUT" on the same record - the
        # decision must use fantasyStatus, not the glitched text.
        injury_detail = {
            "date": "2026-09-01T00:00Z", "status": "7",
            "athlete": {"$ref": "athlete/1"},
            "details": {"fantasyStatus": {"description": "OUT", "abbreviation": "OUT"}, "returnDate": None},
        }
        athlete_detail = {"displayName": "Test Player"}

        def fake_get(url):
            if url == "injuries_page":
                return self._injury_page(["injury/1"])
            if url == "injury/1":
                return injury_detail
            if url == "athlete/1":
                return athlete_detail
            raise AssertionError(f"unexpected url {url}")

        with patch.object(epl, "_fetch_all_injury_refs", return_value=["injury/1"]), \
             patch.object(epl, "_get", side_effect=fake_get):
            results = epl.fetch_team_injuries("nhl", "3")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "Test Player")
        self.assertEqual(results[0]["status"], "OUT")  # display prefers fantasyStatus over the glitched "7"

    def test_active_status_not_flagged(self):
        injury_detail = {
            "date": "2026-09-01T00:00Z", "status": "Active",
            "athlete": {"$ref": "athlete/1"},
            "details": {},
        }

        def fake_get(url):
            if url == "injury/1":
                return injury_detail
            raise AssertionError(f"unexpected url {url}")

        with patch.object(epl, "_fetch_all_injury_refs", return_value=["injury/1"]), \
             patch.object(epl, "_get", side_effect=fake_get):
            results = epl.fetch_team_injuries("nfl", "1")

        self.assertEqual(results, [])

    def test_most_recent_entry_wins_over_older_out_entry(self):
        # A player's log has an OLD "Out" entry and a NEWER "Active" one -
        # must not flag (they're back), even though "Out" appears in
        # their history at all.
        old_out = {
            "date": "2026-08-01T00:00Z", "status": "Out",
            "athlete": {"$ref": "athlete/1"}, "details": {},
        }
        newer_active = {
            "date": "2026-09-01T00:00Z", "status": "Active",
            "athlete": {"$ref": "athlete/1"}, "details": {},
        }

        def fake_get(url):
            return {"injury/old": old_out, "injury/new": newer_active}[url]

        with patch.object(epl, "_fetch_all_injury_refs", return_value=["injury/old", "injury/new"]), \
             patch.object(epl, "_get", side_effect=fake_get):
            results = epl.fetch_team_injuries("nfl", "1")

        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
