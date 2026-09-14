"""Unit tests for scrapers/espn_soccer.py's team-code resolution - the
2026-09-13 "TOR resolved to Real Madrid" incident's regression guard
(coverage audit item 8). Mocks the ESPN HTTP layer (_get) - no live
network calls, no real cache files touched.

Run with: python -m unittest test_espn_soccer -v
"""

import unittest
from unittest.mock import patch

from scrapers import espn_soccer

FAKE_EPL_TEAMS = {
    "sports": [{"leagues": [{"teams": [
        {"team": {"id": "361", "abbreviation": "NEW", "displayName": "Newcastle United"}},
        {"team": {"id": "357", "abbreviation": "LEE", "displayName": "Leeds United"}},
        {"team": {"id": "359", "abbreviation": "ARS", "displayName": "Arsenal"}},
    ]}]}]
}

FAKE_SEA_TEAMS = {
    "sports": [{"leagues": [{"teams": [
        {"team": {"id": "12", "abbreviation": "ROM", "displayName": "AS Roma"}},
        {"team": {"id": "34", "abbreviation": "TOR", "displayName": "Torino"}},
        {"team": {"id": "56", "abbreviation": "INT", "displayName": "Inter Milan"}},
    ]}]}]
}

FAKE_MLS_TEAMS = {
    "sports": [{"leagues": [{"teams": [
        {"team": {"id": "432", "abbreviation": "CHI", "displayName": "Chicago Fire FC"}},
        {"team": {"id": "218", "abbreviation": "SJ", "displayName": "San Jose Earthquakes"}},
    ]}]}]
}


class TestResolveTeamByCode(unittest.TestCase):
    def setUp(self):
        espn_soccer._team_cache.clear()
        espn_soccer._team_raw_cache.clear()

    def test_new_resolves_to_newcastle_united(self):
        with patch.object(espn_soccer, "_get", return_value=FAKE_EPL_TEAMS):
            hit = espn_soccer.resolve_team_by_code("EPL", "NEW")
        self.assertEqual(hit, ("361", "Newcastle United"))

    def test_rom_resolves_to_as_roma(self):
        with patch.object(espn_soccer, "_get", return_value=FAKE_SEA_TEAMS):
            hit = espn_soccer.resolve_team_by_code("SEA", "ROM")
        self.assertEqual(hit, ("12", "AS Roma"))

    def test_tor_resolves_to_torino_never_real_madrid(self):
        """The exact 2026-09-13 incident: soccer_adapter.py once passed
        "TOR" straight to Transfermarkt's free-text search, which
        returned Real Madrid. resolve_team_by_code must resolve "TOR" to
        its real ESPN club (Torino, Serie A) via an exact abbreviation
        match - never a fuzzy/wrong guess."""
        with patch.object(espn_soccer, "_get", return_value=FAKE_SEA_TEAMS):
            hit = espn_soccer.resolve_team_by_code("SEA", "TOR")
        self.assertEqual(hit, ("34", "Torino"))
        self.assertNotIn("real madrid", (hit[1] or "").lower())

    def test_chi_resolves_to_chicago_fire_never_liverpool(self):
        """The other half of the same incident: "CHI" (Chicago Fire, MLS)
        resolved to Liverpool via Transfermarkt's bare-code search."""
        with patch.object(espn_soccer, "_get", return_value=FAKE_MLS_TEAMS):
            hit = espn_soccer.resolve_team_by_code("MLS", "CHI")
        self.assertEqual(hit, ("432", "Chicago Fire FC"))
        self.assertNotIn("liverpool", (hit[1] or "").lower())

    def test_unknown_code_returns_none_not_a_guess(self):
        with patch.object(espn_soccer, "_get", return_value=FAKE_EPL_TEAMS):
            self.assertIsNone(espn_soccer.resolve_team_by_code("EPL", "ZZZ"))

    def test_case_insensitive_match(self):
        with patch.object(espn_soccer, "_get", return_value=FAKE_EPL_TEAMS):
            hit = espn_soccer.resolve_team_by_code("EPL", "new")
        self.assertEqual(hit, ("361", "Newcastle United"))


class TestTeamDisplayName(unittest.TestCase):
    def setUp(self):
        espn_soccer._team_cache.clear()
        espn_soccer._team_raw_cache.clear()

    def test_resolves_real_name_for_transfermarkt_lookup(self):
        with patch.object(espn_soccer, "_get", return_value=FAKE_SEA_TEAMS):
            self.assertEqual(espn_soccer.team_display_name("SEA", "TOR"), "Torino")

    def test_falls_back_to_raw_code_when_unresolved(self):
        """No worse than before this existed - never raises, never blocks
        a candidate from being scored (item 10's watchdog philosophy)."""
        with patch.object(espn_soccer, "_get", return_value=FAKE_EPL_TEAMS):
            self.assertEqual(espn_soccer.team_display_name("EPL", "ZZZ"), "ZZZ")

    def test_substring_fallback_when_exact_abbreviation_mismatches(self):
        """Confirmed live 2026-09-14: Dabble's "ROM" has no exact match
        against ESPN's own Serie A abbreviation for the same club
        ("ROMA", 4 letters) - team_display_name must still resolve it via
        the substring fallback rather than falling through to the raw
        code (which transfermarkt.resolve_team's bare-code guard would
        then correctly, but needlessly, refuse)."""
        teams = {"sports": [{"leagues": [{"teams": [
            {"team": {"id": "104", "abbreviation": "ROMA", "displayName": "AS Roma"}},
            {"team": {"id": "239", "abbreviation": "TOR", "displayName": "Torino"}},
        ]}]}]}
        with patch.object(espn_soccer, "_get", return_value=teams):
            self.assertEqual(espn_soccer.team_display_name("SEA", "ROM"), "AS Roma")


class TestMatchEspnTeamIdPrefersExactCode(unittest.TestCase):
    def setUp(self):
        espn_soccer._team_cache.clear()
        espn_soccer._team_raw_cache.clear()

    def test_exact_code_match_used_over_fuzzy(self):
        with patch.object(espn_soccer, "_get", return_value=FAKE_EPL_TEAMS):
            self.assertEqual(espn_soccer.match_espn_team_id("EPL", "NEW"), "361")

    def test_full_legal_name_still_resolves_via_fuzzy_fallback(self):
        """Older callers (soccer_dns.py's Betr-full-name path) pass a real
        full legal name, not a code - must still work via the substring/
        word-overlap fallback this function always had."""
        with patch.object(espn_soccer, "_get", return_value=FAKE_EPL_TEAMS):
            self.assertEqual(espn_soccer.match_espn_team_id("EPL", "Newcastle United Football Club"), "361")


if __name__ == "__main__":
    unittest.main()
