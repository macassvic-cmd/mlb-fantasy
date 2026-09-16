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


def _match(date, event_id, team_id, started, active=True):
    return {"date": date, "league": "EPL", "event_id": event_id, "team_id": team_id,
            "opponent_team_id": "opp", "competition": "EPL", "home_away": "home",
            "started": started, "active": active}


class TestTeamStartsLastN(unittest.TestCase):
    """2026-09-16 bug fix: starts_last_n's player-appearance-only history
    silently freezes at whatever it was before an injured/dropped player
    stops appearing in squad announcements at all - a team fixture with
    NO entry for that player is invisible to it, not counted as a non-
    start. team_starts_last_n fixes this by deriving the team's real
    fixture list from ANY player's recorded entries for that team_id,
    so an absence (no entry at all) counts as a non-start."""

    def test_the_hinshelwood_case_absences_count_as_non_starts(self):
        # This player (e.g. an injured starter) has 2 real recorded
        # starts, both BEFORE going missing - his own history alone
        # would show 100% starts. Two OTHER players on the same team
        # (team_id "331") have entries for 3 more recent fixtures he
        # is absent from entirely.
        history = {
            "injured player": {"name": "injured player", "matches": [
                _match("2026-05-24", "e1", "331", started=True),
                _match("2026-08-23", "e2", "331", started=True),
            ]},
            "teammate a": {"name": "teammate a", "matches": [
                _match("2026-05-24", "e1", "331", started=True),
                _match("2026-08-23", "e2", "331", started=True),
                _match("2026-08-30", "e3", "331", started=True),
                _match("2026-09-05", "e4", "331", started=True),
                _match("2026-09-13", "e5", "331", started=True),
            ]},
        }
        started, non_started, total = sh.team_starts_last_n(history, "injured player", "331", n=5)
        # Last 5 team fixtures: e1(started) e2(started) e3(absent)
        # e4(absent) e5(absent) -> 2 started, 3 non-started.
        self.assertEqual((started, non_started, total), (2, 3, 5))

    def test_player_with_no_absences_matches_plain_starts_last_n(self):
        history = {
            "regular starter": {"name": "regular starter", "matches": [
                _match("2026-09-01", "e1", "331", started=True),
                _match("2026-09-08", "e2", "331", started=False),
                _match("2026-09-13", "e3", "331", started=True),
            ]},
        }
        started, non_started, total = sh.team_starts_last_n(history, "regular starter", "331", n=3)
        self.assertEqual((started, non_started, total), (2, 1, 3))

    def test_no_team_fixtures_at_all_returns_zero_total(self):
        started, non_started, total = sh.team_starts_last_n({}, "nobody", "331", n=5)
        self.assertEqual((started, non_started, total), (0, 0, 0))

    def test_missing_team_id_returns_zero_total(self):
        history = {"p": {"name": "p", "matches": [_match("2026-09-01", "e1", "331", started=True)]}}
        started, non_started, total = sh.team_starts_last_n(history, "p", None, n=5)
        self.assertEqual((started, non_started, total), (0, 0, 0))

    def test_before_date_excludes_later_fixtures(self):
        history = {
            "p": {"name": "p", "matches": [
                _match("2026-09-01", "e1", "331", started=True),
                _match("2026-09-08", "e2", "331", started=True),
            ]},
        }
        started, non_started, total = sh.team_starts_last_n(history, "p", "331", n=5, before_date="2026-09-05")
        self.assertEqual((started, non_started, total), (1, 0, 1))

    def test_team_confirmation_still_applies_via_expected_team_id(self):
        """A variant-matched player must still pass team confirmation -
        team_starts_last_n must not bypass that safety check."""
        history = {
            "j smith": {"name": "j smith", "matches": [
                _match("2026-09-01", "e1", "999", started=True),  # wrong team
            ]},
            "teammate": {"name": "teammate", "matches": [
                _match("2026-09-01", "e1", "331", started=True),
                _match("2026-09-08", "e2", "331", started=True),
            ]},
        }
        # "john smith" would variant-resolve to "j smith", but that
        # entry's own team_id (999) doesn't match expected_team_id
        # (331) - the variant must be rejected, leaving 0 recorded
        # starts for this player even though the team itself has fixtures.
        started, non_started, total = sh.team_starts_last_n(
            history, "john smith", "331", n=5, expected_team_id="331")
        self.assertEqual((started, non_started, total), (0, 2, 2))


class TestTeamFixtureDetailAndHardOutHelpers(unittest.TestCase):
    """2026-09-16 Hinshelwood follow-up (item 2, hard_out floor): the
    corroboration/conflict checks need per-fixture detail and a real
    "last start date," not just team_starts_last_n's aggregate tally."""

    def _hinshelwood_history(self):
        return {
            "injured player": {"name": "injured player", "matches": [
                _match("2026-05-24", "e1", "331", started=True),
                _match("2026-08-23", "e2", "331", started=True),
            ]},
            "teammate a": {"name": "teammate a", "matches": [
                _match("2026-05-24", "e1", "331", started=True),
                _match("2026-08-23", "e2", "331", started=True),
                _match("2026-08-30", "e3", "331", started=True),
                _match("2026-09-05", "e4", "331", started=True),
                _match("2026-09-13", "e5", "331", started=True),
            ]},
        }

    def test_team_fixture_detail_marks_absences_distinct_from_bench(self):
        history = self._hinshelwood_history()
        history["injured player"]["matches"].append(_match("2026-08-30", "e3", "331", started=False))
        detail = sh.team_fixture_detail(history, "injured player", "331", n=3)
        self.assertEqual([d["event_id"] for d in detail], ["e3", "e4", "e5"])
        self.assertEqual([d["status"] for d in detail], ["bench", "absent", "absent"])

    def test_absent_from_most_recent_team_fixture_true_for_hinshelwood_case(self):
        history = self._hinshelwood_history()
        self.assertTrue(sh.absent_from_most_recent_team_fixture(history, "injured player", "331"))

    def test_absent_from_most_recent_team_fixture_false_when_named(self):
        history = self._hinshelwood_history()
        history["injured player"]["matches"].append(_match("2026-09-13", "e5", "331", started=False))
        self.assertFalse(sh.absent_from_most_recent_team_fixture(history, "injured player", "331"))

    def test_absent_from_most_recent_team_fixture_false_with_no_team_fixtures(self):
        self.assertFalse(sh.absent_from_most_recent_team_fixture({}, "nobody", "331"))

    def test_most_recent_start_date_returns_latest_start_only(self):
        history = {
            "p": {"name": "p", "matches": [
                _match("2026-08-01", "e1", "331", started=True),
                _match("2026-08-15", "e2", "331", started=False),
                _match("2026-08-23", "e3", "331", started=True),
            ]},
        }
        self.assertEqual(sh.most_recent_start_date(history, "p"), "2026-08-23")

    def test_most_recent_start_date_none_when_never_started(self):
        history = {"p": {"name": "p", "matches": [_match("2026-08-01", "e1", "331", started=False)]}}
        self.assertIsNone(sh.most_recent_start_date(history, "p"))

    def test_most_recent_start_date_none_with_no_history(self):
        self.assertIsNone(sh.most_recent_start_date({}, "nobody"))

    def test_last_appearance_date_counts_bench_appearances_too(self):
        history = {
            "p": {"name": "p", "matches": [
                _match("2026-08-01", "e1", "331", started=True),
                _match("2026-08-23", "e2", "331", started=False, active=True),
            ]},
        }
        self.assertEqual(sh.last_appearance_date(history, "p"), "2026-08-23")

    def test_last_appearance_date_ignores_inactive_entries(self):
        history = {
            "p": {"name": "p", "matches": [
                _match("2026-08-01", "e1", "331", started=True),
                _match("2026-08-23", "e2", "331", started=False, active=False),
            ]},
        }
        self.assertEqual(sh.last_appearance_date(history, "p"), "2026-08-01")

    def test_last_appearance_date_none_with_no_history(self):
        self.assertIsNone(sh.last_appearance_date({}, "nobody"))


if __name__ == "__main__":
    unittest.main()
