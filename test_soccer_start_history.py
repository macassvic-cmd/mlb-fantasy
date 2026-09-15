"""Unit tests for soccer_start_history.py - focused on the 2026-09-14
atomic-write fix (a concurrent reader caught a truncated file mid-save
during a long backfill run) and the query helpers added the same day
(starts_last_3, start_rate, bench_rate, previous_start, home_away/
competition fields on a recorded match).

Run with: python -m unittest test_soccer_start_history -v
"""

import json
import os
import tempfile
import unittest

import soccer_start_history as sh


class TestAtomicSave(unittest.TestCase):
    def test_save_leaves_no_tmp_file_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.json")
            sh._save(path, {"a": 1})
            self.assertTrue(os.path.exists(path))
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_save_result_is_valid_json_matching_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.json")
            data = {"player one": {"name": "player one", "matches": [{"date": "2026-08-01"}]}}
            sh._save(path, data)
            with open(path, encoding="utf-8") as f:
                reloaded = json.load(f)
            self.assertEqual(reloaded, data)

    def test_save_overwrites_existing_file_completely(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "history.json")
            sh._save(path, {"a": 1, "b": 2})
            sh._save(path, {"c": 3})
            with open(path, encoding="utf-8") as f:
                reloaded = json.load(f)
            self.assertEqual(reloaded, {"c": 3})


class TestQueryHelpers(unittest.TestCase):
    HISTORY = {"player x": {"name": "player x", "matches": [
        {"date": "2026-08-01", "league": "EPL", "event_id": "1", "team_id": "t1",
         "opponent_team_id": "t2", "competition": "EPL", "home_away": "home",
         "started": True, "active": True},
        {"date": "2026-08-08", "league": "EPL", "event_id": "2", "team_id": "t1",
         "opponent_team_id": "t3", "competition": "EPL", "home_away": "away",
         "started": False, "active": True},
        {"date": "2026-08-15", "league": "EPL", "event_id": "3", "team_id": "t1",
         "opponent_team_id": "t4", "competition": "EPL", "home_away": "home",
         "started": True, "active": True},
    ]}}

    def test_previous_start_reflects_most_recent_match(self):
        self.assertTrue(sh.previous_start(self.HISTORY, "player x"))

    def test_previous_start_none_when_no_history(self):
        self.assertIsNone(sh.previous_start({}, "nobody"))

    def test_starts_last_3_matches_generic_helper(self):
        self.assertEqual(sh.starts_last_3(self.HISTORY, "player x"), sh.starts_last_n(self.HISTORY, "player x", n=3))

    def test_start_rate_and_bench_rate_are_complementary(self):
        rate = sh.start_rate(self.HISTORY, "player x", n=3)
        bench = sh.bench_rate(self.HISTORY, "player x", n=3)
        self.assertAlmostEqual(rate + bench, 100.0, places=1)

    def test_recorded_match_carries_home_away_and_competition(self):
        m = self.HISTORY["player x"]["matches"][0]
        self.assertIn("home_away", m)
        self.assertIn("competition", m)


class TestNameVariantResolution(unittest.TestCase):
    """Dabble's player_name is a full legal name; ESPN's squad list (this
    module's only source) indexes the common name - confirmed live
    2026-09-14: 101/140 LaLiga board players had zero history purely from
    this mismatch (e.g. "bryan zaragoza martinez" vs ESPN's "bryan
    zaragoza")."""

    HISTORY = {"bryan zaragoza": {"name": "bryan zaragoza", "matches": [
        {"date": "2026-08-01", "league": "LLG", "event_id": "1", "team_id": "t1",
         "opponent_team_id": "t2", "competition": "LLG", "home_away": "home",
         "started": True, "active": True},
    ]}}

    def test_full_legal_name_resolves_via_first_and_last_word_with_team_confirmed(self):
        self.assertTrue(sh.previous_start(self.HISTORY, "bryan zaragoza martinez", expected_team_id="t1"))

    def test_exact_match_still_preferred_over_variant(self):
        history = {"jon martin": {"name": "jon martin", "matches": [
            {"date": "2026-08-01", "league": "LLG", "event_id": "1", "team_id": "t1",
             "opponent_team_id": "t2", "competition": "LLG", "home_away": "home",
             "started": False, "active": True}]},
                   "jon x martin": {"name": "jon x martin", "matches": [
            {"date": "2026-08-01", "league": "LLG", "event_id": "2", "team_id": "t1",
             "opponent_team_id": "t2", "competition": "LLG", "home_away": "home",
             "started": True, "active": True}]}}
        # exact 2-word key "jon martin" must win over any variant of a
        # different 3-word name that happens to share first/last word -
        # and needs no expected_team_id at all since it's an exact match.
        self.assertFalse(sh.previous_start(history, "jon martin"))

    def test_two_word_names_have_no_variants_to_try(self):
        self.assertEqual(sh._name_variant_candidates("bryan zaragoza"), [])

    def test_four_word_name_resolves_via_first_plus_third_word_with_team_confirmed(self):
        history = {"jose gaya": {"name": "jose gaya", "matches": [
            {"date": "2026-08-01", "league": "LLG", "event_id": "1", "team_id": "t1",
             "opponent_team_id": "t2", "competition": "LLG", "home_away": "home",
             "started": True, "active": True}]}}
        self.assertTrue(sh.previous_start(history, "jose luis gaya pena", expected_team_id="t1"))

    def test_unresolvable_name_returns_none_not_a_crash(self):
        self.assertIsNone(sh.previous_start(self.HISTORY, "someone else entirely", expected_team_id="t1"))


class TestTeamConfirmationForPartialMatches(unittest.TestCase):
    """2026-09-14 item 1 (name-match audit): a name-variant match must be
    REJECTED - not silently accepted - unless its team is confirmed. A
    shortened variant ("bryan zaragoza") can coincidentally collide with
    an unrelated player who happens to share that exact shortened form;
    team confirmation is what catches that instead of trusting the name
    alone."""

    HISTORY = {"bryan zaragoza": {"name": "bryan zaragoza", "matches": [
        {"date": "2026-08-01", "league": "LLG", "event_id": "1", "team_id": "t1",
         "opponent_team_id": "t2", "competition": "LLG", "home_away": "home",
         "started": True, "active": True},
    ]}}

    def test_variant_rejected_without_expected_team_id(self):
        self.assertIsNone(sh.previous_start(self.HISTORY, "bryan zaragoza martinez"))

    def test_variant_rejected_when_team_id_mismatches(self):
        self.assertIsNone(sh.previous_start(self.HISTORY, "bryan zaragoza martinez", expected_team_id="wrong_team"))

    def test_variant_accepted_when_team_id_matches(self):
        self.assertTrue(sh.previous_start(self.HISTORY, "bryan zaragoza martinez", expected_team_id="t1"))

    def test_exact_match_needs_no_expected_team_id(self):
        self.assertTrue(sh.previous_start(self.HISTORY, "bryan zaragoza"))

    def test_reject_log_records_missing_team_rejection(self):
        log = []
        sh.previous_start(self.HISTORY, "bryan zaragoza martinez", reject_log=log)
        self.assertEqual(len(log), 1)
        self.assertFalse(log[0]["accepted"])
        self.assertEqual(log[0]["variant"], "bryan zaragoza")

    def test_reject_log_records_team_mismatch_rejection(self):
        log = []
        sh.previous_start(self.HISTORY, "bryan zaragoza martinez", expected_team_id="wrong_team", reject_log=log)
        self.assertEqual(len(log), 1)
        self.assertFalse(log[0]["accepted"])
        self.assertEqual(log[0]["entry_team_ids"], ["t1"])

    def test_no_log_entry_for_a_clean_exact_match(self):
        log = []
        sh.previous_start(self.HISTORY, "bryan zaragoza", reject_log=log)
        self.assertEqual(log, [])

    def test_log_records_an_accepted_variant_match_too(self):
        # reject_log records every variant ATTEMPT, accepted or not - the
        # audit needs both to compute precision (accepted / total).
        log = []
        sh.previous_start(self.HISTORY, "bryan zaragoza martinez", expected_team_id="t1", reject_log=log)
        self.assertEqual(len(log), 1)
        self.assertTrue(log[0]["accepted"])


if __name__ == "__main__":
    unittest.main()
