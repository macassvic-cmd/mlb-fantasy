"""Unit tests for rotowire_soccer.py's player-page resolution, focused on
the 2026-09-14 coverage-depth fix: Dabble's soccer player_name is the
player's full LEGAL name ("Anthony Michael Gordon"), not the common
football name RotoWire indexes ("Anthony Gordon") - resolve_player_page
must try both. Confirmed live this took real match rate from 42% to 87%
on the actual current Dabble board.

No real network call - build_player_index is always given an explicit
fixture index.

Run with: python -m unittest test_rotowire_soccer -v
"""

import unittest
from unittest.mock import patch

import rotowire_soccer as rw

FIXTURE_INDEX = {
    "anthony gordon": [{"url": "https://www.rotowire.com/soccer/player/anthony-gordon-1", "player_id": "1"}],
    "cole palmer": [{"url": "https://www.rotowire.com/soccer/player/cole-palmer-2", "player_id": "2"}],
    "eduardo camavinga": [{"url": "https://www.rotowire.com/soccer/player/eduardo-camavinga-3", "player_id": "3"}],
    "amar dedic": [{"url": "https://www.rotowire.com/soccer/player/amar-dedic-4", "player_id": "4"}],
    "ambiguous player": [
        {"url": "https://www.rotowire.com/soccer/player/ambiguous-player-5", "player_id": "5"},
        {"url": "https://www.rotowire.com/soccer/player/ambiguous-player-6", "player_id": "6"},
    ],
}


class TestNameVariantCandidates(unittest.TestCase):
    def test_full_legal_name_generates_first_plus_last(self):
        variants = rw._name_variant_candidates("Anthony Michael Gordon")
        self.assertIn("Anthony Gordon", variants)

    def test_three_word_name_also_tries_spanish_paternal_surname_form(self):
        variants = rw._name_variant_candidates("Fermin Lopez Marin")
        self.assertIn("Fermin Lopez", variants)  # first + second word
        self.assertIn("Fermin Marin", variants)  # first + last word

    def test_two_word_name_variant_is_a_noop_not_a_wrong_shortening(self):
        # Already the shortest plausible form - first+last IS the name
        # itself here, so this can't accidentally shorten a real 2-word
        # name into something wrong.
        self.assertEqual(rw._name_variant_candidates("Amar Dedic"), ["Amar Dedic"])

    def test_single_word_name_generates_no_variants(self):
        self.assertEqual(rw._name_variant_candidates("Neymar"), [])


class TestResolvePlayerPageFallback(unittest.TestCase):
    def test_exact_full_name_match_used_first(self):
        hit = rw.resolve_player_page("Amar Dedic", index=FIXTURE_INDEX)
        self.assertEqual(hit, ("https://www.rotowire.com/soccer/player/amar-dedic-4", "4", False, False))

    def test_full_legal_name_falls_back_to_first_plus_last(self):
        """The exact bug found live: 'Anthony Michael Gordon' has no
        exact-match entry, but 'Anthony Gordon' does."""
        hit = rw.resolve_player_page("Anthony Michael Gordon", index=FIXTURE_INDEX)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], "1")
        self.assertTrue(hit[3], "a variant-derived hit must be flagged via_variant=True")

    def test_exact_match_is_not_flagged_as_a_variant(self):
        hit = rw.resolve_player_page("Amar Dedic", index=FIXTURE_INDEX)
        self.assertFalse(hit[3])

    def test_camavinga_style_middle_name_insert_resolves_via_first_plus_last(self):
        hit = rw.resolve_player_page("Eduardo Celmi Camavinga", index=FIXTURE_INDEX)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], "3")

    def test_no_match_at_all_returns_none(self):
        self.assertIsNone(rw.resolve_player_page("Totally Fake Nobody Player", index=FIXTURE_INDEX))

    def test_ambiguous_full_name_flagged(self):
        hit = rw.resolve_player_page("Ambiguous Player", index=FIXTURE_INDEX)
        self.assertTrue(hit[2])

    def test_never_fabricates_a_close_enough_match(self):
        """A near-miss name with NO exact match at any variant level must
        stay unmatched, never guessed."""
        index = {"cole palmer": FIXTURE_INDEX["cole palmer"]}
        self.assertIsNone(rw.resolve_player_page("Colin Palmerson", index=index))


class TestParsePlayerTeam(unittest.TestCase):
    def test_parses_team_and_league_from_real_page_layout(self):
        html = ('<div>...<span>F/M</span></div> <div style="font-size: 14px;font-weight:700">'
                'Espanyol</div><div style="font-size: 14px;">La Liga</div>')
        self.assertEqual(rw._parse_player_team(html), ("Espanyol", "La Liga"))

    def test_missing_block_returns_none_none_not_a_crash(self):
        self.assertEqual(rw._parse_player_team("<html>nothing here</html>"), (None, None))


class TestTeamNamesMatch(unittest.TestCase):
    def test_exact_match(self):
        self.assertTrue(rw._team_names_match("Espanyol", "Espanyol"))

    def test_substring_tolerant_both_directions(self):
        self.assertTrue(rw._team_names_match("Barcelona", "FC Barcelona"))
        self.assertTrue(rw._team_names_match("FC Barcelona", "Barcelona"))

    def test_diacritics_and_case_ignored(self):
        self.assertTrue(rw._team_names_match("ATLETICO MADRID", "Atlético Madrid"))

    def test_different_clubs_do_not_match(self):
        self.assertFalse(rw._team_names_match("Espanyol", "Sevilla"))

    def test_missing_either_side_never_matches(self):
        self.assertFalse(rw._team_names_match(None, "Espanyol"))
        self.assertFalse(rw._team_names_match("Espanyol", None))
        self.assertFalse(rw._team_names_match(None, None))


class TestGetPlayerStatusTeamConfirmation(unittest.TestCase):
    """2026-09-14 item 1: a partial (name-variant) match must be REJECTED
    - not silently accepted - when the page's own team doesn't confirm
    it, or when there's no expected_team to check against at all."""

    def _patch_variant_hit(self, page_team="Espanyol"):
        hit = ("https://www.rotowire.com/soccer/player/x-1", "1", False, True)  # via_variant=True
        html = ('<div style="font-size: 14px;font-weight:700">' + (page_team or "") +
                '</div><div style="font-size: 14px;">La Liga</div>') if page_team else "<html></html>"
        return patch("rotowire_soccer.resolve_player_page", return_value=hit), \
            patch("rotowire_soccer._fetch_url", return_value=html)

    def test_variant_match_confirmed_when_team_agrees(self):
        p1, p2 = self._patch_variant_hit(page_team="Espanyol")
        with p1, p2:
            result = rw.get_player_status("Some Legal Name", expected_team="Espanyol", use_disk_cache=False)
        self.assertTrue(result["matched"])
        self.assertTrue(result["team_confirmed"])

    def test_variant_match_rejected_when_team_disagrees(self):
        p1, p2 = self._patch_variant_hit(page_team="Sevilla")
        with p1, p2:
            result = rw.get_player_status("Some Legal Name", expected_team="Espanyol", use_disk_cache=False)
        self.assertFalse(result["matched"], "a team mismatch on a partial match must be rejected, not accepted")
        self.assertFalse(result["team_confirmed"])

    def test_variant_match_rejected_when_expected_team_missing(self):
        p1, p2 = self._patch_variant_hit(page_team="Espanyol")
        with p1, p2:
            result = rw.get_player_status("Some Legal Name", expected_team=None, use_disk_cache=False)
        self.assertFalse(result["matched"], "no team to confirm against must reject, never accept blind")

    def test_variant_match_rejected_when_page_team_unavailable(self):
        p1, p2 = self._patch_variant_hit(page_team=None)
        with p1, p2:
            result = rw.get_player_status("Some Legal Name", expected_team="Espanyol", use_disk_cache=False)
        self.assertFalse(result["matched"])

    def test_exact_match_needs_no_team_confirmation(self):
        hit = ("https://www.rotowire.com/soccer/player/x-1", "1", False, False)  # via_variant=False
        with patch("rotowire_soccer.resolve_player_page", return_value=hit), \
             patch("rotowire_soccer._fetch_url", return_value="<html></html>"):
            result = rw.get_player_status("Exact Name", expected_team=None, use_disk_cache=False)
        self.assertTrue(result["matched"], "an exact full-name match needs no team check at all")
        self.assertIsNone(result["team_confirmed"], "team_confirmed is not applicable to an exact match")

    def test_match_log_records_rejected_and_accepted_attempts(self):
        p1, p2 = self._patch_variant_hit(page_team="Sevilla")
        log = []
        with p1, p2:
            rw.get_player_status("Some Legal Name", expected_team="Espanyol", use_disk_cache=False, match_log=log)
        self.assertEqual(len(log), 1)
        self.assertFalse(log[0]["accepted"])
        self.assertEqual(log[0]["page_team"], "Sevilla")
        self.assertEqual(log[0]["expected_team"], "Espanyol")


if __name__ == "__main__":
    unittest.main()
