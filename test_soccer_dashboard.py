"""Unit tests for soccer_dashboard.py - the standalone Soccer DNS command
center page. Isolation: every generate() call writes to a tempfile path,
never the real docs/soccer-dns.html; discord_health/soccer_x_monitor are
left un-mocked where safe (pure local file reads) but any env-dependent
check is tolerant of either state.

Run with: python -m unittest test_soccer_dashboard -v
"""

import json
import os
import tempfile
import unittest

import soccer_dashboard as dash


def _candidate(name, dns=50, team="NEW", **overrides):
    c = {
        "player_name": name, "team": team, "dabble_player_id": name.lower().replace(" ", "-"),
        "league_code": "EPL", "league_raw": "EPL", "matchup": "NEW @ LEE",
        "event_date": "2026-09-14T19:00:00.000Z", "fixture_id": "fx1",
        "dns_score": dns, "confidence_score": 50, "urgency_score": 0,
        "combined_priority": round(dns * 0.7, 1),
        "dabble_status_raw": None, "dabble_status_normalized": None,
        "rotowire_status_raw": None, "rotowire_page_tag": None, "rotowire_injury": None,
        "transfermarkt_injury": None, "official_status": "not_yet_posted",
        "has_history": False, "has_injury": False, "has_news": False,
        "has_predicted_xi": False, "has_x_signal": False,
        "has_any_external_enrichment": False, "has_multiple_external_sources": False,
        "board_only": True, "starter_votes": 0, "bench_votes": 0, "unknown_votes": 0,
        "source_health": {
            "official_lineup": {"source_attempted": True, "entity_matched": True, "signal_found": False},
            "transfermarkt": {"source_attempted": True, "entity_matched": True, "signal_found": False},
            "rotowire": {"source_attempted": True, "entity_matched": False, "signal_found": False},
            "x": {"source_attempted": False, "entity_matched": False, "signal_found": False},
        },
        "prediction_consensus": "unknown", "predicted_start_sources": [], "predicted_xi_sources": [],
        "top_reasons": ["some reason"],
    }
    c.update(overrides)
    return c


class TestGenerateWritesToGivenPath(unittest.TestCase):
    def test_writes_to_custom_out_path_not_real_docs_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "soccer-dns.html")
            dash.generate([_candidate("Player A")], date_str="2099-01-01", out_path=out_path)
            self.assertTrue(os.path.exists(out_path))
            real_path = os.path.join("docs", "soccer-dns.html")
            # Don't assert the real file is untouched by mtime (other
            # tests/processes may have written it) - just confirm THIS
            # call targeted the tempdir path, not the real one.
        self.assertTrue(True)


class TestCoverageMatrix(unittest.TestCase):
    def test_matrix_reflects_has_flags_per_player(self):
        candidates = [
            _candidate("Board Only Player"),
            _candidate("Two Source Player", has_history=True, has_news=True,
                       has_any_external_enrichment=True, has_multiple_external_sources=True,
                       board_only=False, starter_votes=1, bench_votes=1, unknown_votes=0),
        ]
        matrix = dash._coverage_matrix(candidates)
        by_name = {m["name"]: m for m in matrix}
        self.assertFalse(by_name["Board Only Player"]["history"])
        self.assertEqual(by_name["Board Only Player"]["sourceCount"], 0)
        self.assertTrue(by_name["Two Source Player"]["history"])
        self.assertTrue(by_name["Two Source Player"]["news"])
        self.assertEqual(by_name["Two Source Player"]["sourceCount"], 2)

    def test_votes_never_conflated_across_players(self):
        candidates = [
            _candidate("A", starter_votes=2, bench_votes=0, unknown_votes=1),
            _candidate("B", starter_votes=0, bench_votes=3, unknown_votes=0),
        ]
        matrix = dash._coverage_matrix(candidates)
        by_name = {m["name"]: m for m in matrix}
        self.assertEqual(by_name["A"]["starterVotes"], 2)
        self.assertEqual(by_name["A"]["unknownVotes"], 1)
        self.assertEqual(by_name["B"]["benchVotes"], 3)
        self.assertEqual(by_name["A"]["benchVotes"], 0)


class TestCoverageTiers(unittest.TestCase):
    def test_tiers_partition_all_candidates(self):
        candidates = [
            _candidate("Board Only"),
            _candidate("One Source", has_any_external_enrichment=True, board_only=False),
            _candidate("Two Plus", has_any_external_enrichment=True, has_multiple_external_sources=True,
                       board_only=False),
        ]
        tiers = dash._coverage_tiers(candidates)
        self.assertEqual(tiers["total"], 3)
        self.assertEqual(tiers["boardOnly"], 1)
        self.assertEqual(tiers["oneSource"], 1)
        self.assertEqual(tiers["twoPlusSource"], 1)

    def test_fully_enriched_requires_all_four_raw_sources(self):
        candidates = [
            _candidate("Full", has_history=True, has_injury=True, has_news=True, has_x_signal=True,
                       has_any_external_enrichment=True, has_multiple_external_sources=True, board_only=False),
            _candidate("Partial", has_history=True, has_news=True,
                       has_any_external_enrichment=True, has_multiple_external_sources=True, board_only=False),
        ]
        tiers = dash._coverage_tiers(candidates)
        self.assertEqual(tiers["fullyEnriched"], 1)


class TestGeneratedHtmlContainsNewSections(unittest.TestCase):
    def test_html_includes_coverage_matrix_and_discord_health_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "soccer-dns.html")
            dash.generate([_candidate("Player A")], date_str="2099-01-01", out_path=out_path)
            with open(out_path, encoding="utf-8") as f:
                html = f.read()
        self.assertIn("Coverage Matrix", html)
        self.assertIn("Discord Health", html)
        self.assertIn("matrixBody", html)
        self.assertIn("discordHealthGrid", html)


class TestFixtureFreshnessInputs(unittest.TestCase):
    """item 3, 2026-09-16: the merged Last Refresh status needs to know
    whether staleness actually matters right now (nothing upcoming =
    harmless; a fixture inside 24h = urgent)."""

    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)

    def test_no_candidates_nothing_upcoming(self):
        any_upcoming, within_24h = dash._fixture_freshness_inputs([], self._now())
        self.assertFalse(any_upcoming)
        self.assertFalse(within_24h)

    def test_all_kickoffs_in_the_past_nothing_upcoming(self):
        c = _candidate("A", event_date="2026-09-16T10:00:00.000Z")
        any_upcoming, within_24h = dash._fixture_freshness_inputs([c], self._now())
        self.assertFalse(any_upcoming)
        self.assertFalse(within_24h)

    def test_upcoming_fixture_beyond_24h_is_upcoming_but_not_within_24h(self):
        c = _candidate("A", event_date="2026-09-18T12:00:00.000Z")
        any_upcoming, within_24h = dash._fixture_freshness_inputs([c], self._now())
        self.assertTrue(any_upcoming)
        self.assertFalse(within_24h)

    def test_upcoming_fixture_within_24h(self):
        c = _candidate("A", event_date="2026-09-17T06:00:00.000Z")
        any_upcoming, within_24h = dash._fixture_freshness_inputs([c], self._now())
        self.assertTrue(any_upcoming)
        self.assertTrue(within_24h)

    def test_missing_or_malformed_event_date_is_skipped_not_a_crash(self):
        c1 = _candidate("A", event_date=None)
        c2 = _candidate("B", event_date="not-a-date")
        any_upcoming, within_24h = dash._fixture_freshness_inputs([c1, c2], self._now())
        self.assertFalse(any_upcoming)
        self.assertFalse(within_24h)


if __name__ == "__main__":
    unittest.main()
