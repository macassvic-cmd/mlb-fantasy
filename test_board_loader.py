"""Unit tests for board_loader.py. Run with: python -m unittest test_board_loader -v"""

import unittest

from board_loader import load_prop_records, to_mlb_betr_entries, to_soccer_board


class TestFieldAliasing(unittest.TestCase):
    def test_canonical_field_names(self):
        records = load_prop_records([
            {"player_name": "Aaron Judge", "team": "NYY", "market": "HITS", "line": 1.5,
             "event_date": "2026-09-10T23:05:00Z", "sport": "mlb"},
        ])
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual(r.player_name, "Aaron Judge")
        self.assertEqual(r.normalized_name, "aaron judge")
        self.assertEqual(r.team, "NYY")
        self.assertEqual(r.market, "HITS")
        self.assertEqual(r.line, 1.5)
        self.assertEqual(r.sport, "mlb")

    def test_alias_field_names(self):
        records = load_prop_records([
            {"player": "Aaron Judge", "team_name": "NYY", "stat": "hits", "value": 1.5,
             "date": "2026-09-10T23:05:00Z", "league_sport": "MLB"},
        ])
        r = records[0]
        self.assertEqual(r.player_name, "Aaron Judge")
        self.assertEqual(r.team, "NYY")
        self.assertEqual(r.market, "HITS")  # uppercased
        self.assertEqual(r.sport, "mlb")    # lowercased

    def test_optional_fields_default_none(self):
        records = load_prop_records([
            {"player_name": "X", "team": "NYY", "market": "HITS", "line": 1.5,
             "event_date": "2026-09-10", "sport": "mlb"},
        ])
        self.assertIsNone(records[0].opponent)
        self.assertIsNone(records[0].odds)

    def test_optional_fields_populated_when_present(self):
        records = load_prop_records([
            {"player_name": "X", "team": "NYY", "opponent": "BOS", "market": "HITS", "line": 1.5,
             "odds": -120, "event_date": "2026-09-10", "sport": "mlb"},
        ])
        self.assertEqual(records[0].opponent, "BOS")
        self.assertEqual(records[0].odds, -120.0)


class TestSportResolution(unittest.TestCase):
    def test_file_level_sport_wrapper(self):
        data = {"sport": "soccer", "props": [
            {"player_name": "X", "team": "Arsenal", "market": "SHOTS", "line": 1.5, "event_date": "2026-09-10"},
        ]}
        records = load_prop_records(data)
        self.assertEqual(records[0].sport, "soccer")

    def test_per_record_sport_overrides_file_level(self):
        data = {"sport": "soccer", "props": [
            {"player_name": "X", "team": "NYY", "market": "HITS", "line": 1.5,
             "event_date": "2026-09-10", "sport": "mlb"},
        ]}
        records = load_prop_records(data)
        self.assertEqual(records[0].sport, "mlb")

    def test_sport_hint_used_when_no_sport_field_anywhere(self):
        records = load_prop_records(
            [{"player_name": "X", "team": "NYY", "market": "HITS", "line": 1.5, "event_date": "2026-09-10"}],
            sport_hint="mlb",
        )
        self.assertEqual(records[0].sport, "mlb")

    def test_missing_sport_with_no_hint_rejected(self):
        with self.assertRaises(ValueError):
            load_prop_records(
                [{"player_name": "X", "team": "NYY", "market": "HITS", "line": 1.5, "event_date": "2026-09-10"}]
            )


class TestValidation(unittest.TestCase):
    def test_missing_required_field_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            load_prop_records([{"player_name": "X", "team": "NYY", "market": "HITS", "event_date": "2026-09-10"}],
                               sport_hint="mlb")
        self.assertIn("line", str(ctx.exception))

    def test_non_numeric_line_rejected(self):
        with self.assertRaises(ValueError):
            load_prop_records(
                [{"player_name": "X", "team": "NYY", "market": "HITS", "line": "not_a_number", "event_date": "2026-09-10"}],
                sport_hint="mlb",
            )

    def test_bad_odds_does_not_fail_whole_record(self):
        # odds is cosmetic - a garbage value there shouldn't kill the record.
        records = load_prop_records(
            [{"player_name": "X", "team": "NYY", "market": "HITS", "line": 1.5,
              "odds": "n/a", "event_date": "2026-09-10"}],
            sport_hint="mlb",
        )
        self.assertIsNone(records[0].odds)

    def test_non_object_record_rejected(self):
        with self.assertRaises(ValueError):
            load_prop_records(["not_an_object"], sport_hint="mlb")

    def test_non_array_non_object_top_level_rejected(self):
        # A bare string is treated as a FILE PATH (see load_prop_records'
        # source-type check), so this needs a genuinely non-string,
        # non-list, non-dict in-memory value to exercise the "wrong
        # top-level JSON shape" branch rather than "file not found."
        with self.assertRaises(ValueError):
            load_prop_records(12345)

    def test_object_without_props_key_rejected(self):
        with self.assertRaises(ValueError):
            load_prop_records({"sport": "mlb"})


class TestToMlbBetrEntries(unittest.TestCase):
    def test_shape_matches_betr_fetcher(self):
        records = load_prop_records([
            {"player_name": "Aaron Judge", "team": "NYY", "market": "HITS", "line": 1.5,
             "event_date": "2026-09-10T23:05:00Z", "sport": "mlb"},
            {"player_name": "Aaron Judge", "team": "NYY", "market": "TOTAL_BASES", "line": 2.5,
             "event_date": "2026-09-10T23:05:00Z", "sport": "mlb"},
        ])
        entries = to_mlb_betr_entries(records)
        self.assertEqual(len(entries), 1)  # merged - same player, two markets
        e = entries[0]
        self.assertEqual(set(e.keys()), {"name", "normalized_name", "team", "event_date_utc", "markets"})
        self.assertEqual(e["markets"], {"HITS": 1.5, "TOTAL_BASES": 2.5})

    def test_non_mlb_records_excluded(self):
        records = load_prop_records([
            {"player_name": "X", "team": "NYY", "market": "HITS", "line": 1.5, "event_date": "2026-09-10", "sport": "mlb"},
            {"player_name": "Y", "team": "Arsenal", "market": "SHOTS", "line": 1.5, "event_date": "2026-09-10", "sport": "soccer"},
        ])
        entries = to_mlb_betr_entries(records)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["name"], "X")


class TestToSoccerBoard(unittest.TestCase):
    def test_groups_by_fixture(self):
        records = load_prop_records([
            {"player_name": "Player A", "team": "Arsenal", "opponent": "Chelsea",
             "market": "SHOTS", "line": 1.5, "event_date": "2026-09-14T15:00:00Z", "sport": "soccer"},
            {"player_name": "Player B", "team": "Chelsea", "opponent": "Arsenal",
             "market": "TACKLES", "line": 2.5, "event_date": "2026-09-14T15:00:00Z", "sport": "soccer"},
        ])
        board = to_soccer_board(records)
        self.assertEqual(len(board), 1)  # both records are the same fixture
        event = list(board.values())[0]
        self.assertEqual(set(event["teams"].keys()), {"Arsenal", "Chelsea"})
        self.assertIsNone(event["league"])

    def test_different_dates_are_different_fixtures(self):
        records = load_prop_records([
            {"player_name": "A", "team": "Arsenal", "opponent": "Chelsea",
             "market": "SHOTS", "line": 1.5, "event_date": "2026-09-14T15:00:00Z", "sport": "soccer"},
            {"player_name": "A", "team": "Arsenal", "opponent": "Chelsea",
             "market": "SHOTS", "line": 1.5, "event_date": "2026-10-01T15:00:00Z", "sport": "soccer"},
        ])
        board = to_soccer_board(records)
        self.assertEqual(len(board), 2)

    def test_sparse_one_sided_file_still_registers_opponent(self):
        # Only ONE side's player is in the file - the opponent must
        # still appear as a (possibly empty) key in ev["teams"], or
        # soccer_dns.py's other_team lookup resolves to None and every
        # flag's "opponent" field silently goes blank (found live
        # 2026-09-08 - also broke the individual Discord post, since
        # _flag_embed formats a None league the same way).
        records = load_prop_records([
            {"player_name": "A", "team": "Arsenal", "opponent": "Chelsea",
             "market": "SHOTS", "line": 1.5, "event_date": "2026-09-14", "sport": "soccer"},
        ])
        board = to_soccer_board(records)
        event = list(board.values())[0]
        self.assertIn("Chelsea", event["teams"])
        self.assertEqual(event["teams"]["Chelsea"], {})

    def test_league_field_carried_through(self):
        records = load_prop_records([
            {"player_name": "A", "team": "Arsenal", "opponent": "Chelsea", "league": "epl",
             "market": "SHOTS", "line": 1.5, "event_date": "2026-09-14", "sport": "soccer"},
        ])
        self.assertEqual(records[0].league, "EPL")
        board = to_soccer_board(records)
        event = list(board.values())[0]
        self.assertEqual(event["league"], "EPL")

    def test_league_defaults_to_none_when_absent(self):
        records = load_prop_records([
            {"player_name": "A", "team": "Arsenal", "opponent": "Chelsea",
             "market": "SHOTS", "line": 1.5, "event_date": "2026-09-14", "sport": "soccer"},
        ])
        self.assertIsNone(records[0].league)
        board = to_soccer_board(records)
        event = list(board.values())[0]
        self.assertIsNone(event["league"])

    def test_markets_carry_line_values(self):
        records = load_prop_records([
            {"player_name": "A", "team": "Arsenal", "opponent": "Chelsea",
             "market": "SHOTS", "line": 1.5, "event_date": "2026-09-14", "sport": "soccer"},
        ])
        board = to_soccer_board(records)
        event = list(board.values())[0]
        player_entry = list(event["teams"]["Arsenal"].values())[0]
        self.assertEqual(player_entry["markets"], {"SHOTS": 1.5})
        # sorted()/`in` over this dict must behave like the old set-of-keys shape:
        self.assertEqual(sorted(player_entry["markets"]), ["SHOTS"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
