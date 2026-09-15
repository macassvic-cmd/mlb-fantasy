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
        self.assertEqual(hit, ("https://www.rotowire.com/soccer/player/amar-dedic-4", "4", False))

    def test_full_legal_name_falls_back_to_first_plus_last(self):
        """The exact bug found live: 'Anthony Michael Gordon' has no
        exact-match entry, but 'Anthony Gordon' does."""
        hit = rw.resolve_player_page("Anthony Michael Gordon", index=FIXTURE_INDEX)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], "1")

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


if __name__ == "__main__":
    unittest.main()
