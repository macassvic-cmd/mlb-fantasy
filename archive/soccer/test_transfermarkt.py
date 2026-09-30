"""Unit tests for scrapers/transfermarkt.py's bare-board-code guard
(coverage audit item 8) - resolve_team must refuse to search/cache a
short code like "TOR" rather than repeat the 2026-09-13 incident where
that search returned Real Madrid and got cached as if verified.

Mocks the HTTP layer and cache file path - no live network calls, no
real data/transfermarkt/ files touched.

Run with: python -m unittest test_transfermarkt -v
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from scrapers import transfermarkt as tm


class TestBareBoardCodeGuard(unittest.TestCase):
    def test_short_uppercase_code_is_refused(self):
        for code in ("TOR", "NEW", "CHI", "ROM", "LEE", "INT"):
            with self.subTest(code=code):
                self.assertTrue(tm._looks_like_a_bare_board_code(code))

    def test_real_club_names_are_not_flagged(self):
        for name in ("Newcastle United", "AS Roma", "Torino", "Chicago Fire FC", "Real Madrid"):
            with self.subTest(name=name):
                self.assertFalse(tm._looks_like_a_bare_board_code(name))

    def test_resolve_team_refuses_bare_code_without_searching_or_caching(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "team_resolution.json")
            with patch.object(tm, "TEAM_CACHE_PATH", cache_path), \
                 patch.object(tm, "_search_team_once") as mock_search:
                slug, team_id = tm.resolve_team("TOR")
                self.assertIsNone(slug)
                self.assertIsNone(team_id)
                mock_search.assert_not_called()
                self.assertFalse(os.path.exists(cache_path), "a refused code must never be written to the cache")

    def test_resolve_team_still_searches_a_real_club_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "team_resolution.json")
            with patch.object(tm, "TEAM_CACHE_PATH", cache_path), \
                 patch.object(tm, "_search_team_once", return_value=("/torino", "34")) as mock_search:
                slug, team_id = tm.resolve_team("Torino")
                self.assertEqual((slug, team_id), ("/torino", "34"))
                mock_search.assert_called_once_with("Torino")

    def test_get_team_injuries_cached_returns_empty_for_bare_code_never_poisons_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = os.path.join(tmp, "team_resolution.json")
            with patch.object(tm, "TEAM_CACHE_PATH", cache_path), \
                 patch.object(tm, "CACHE_DIR", tmp):
                result = tm.get_team_injuries_cached("TOR", "2026-09-14")
                self.assertEqual(result, [])
                self.assertFalse(os.path.exists(cache_path))


if __name__ == "__main__":
    unittest.main()
