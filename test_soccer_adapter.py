"""Unit tests for soccer_adapter.py - includes the Amar Dedic / Mama Balde
regression fixtures from the 2026-09-14 audit (why they were missing from
DNS, and proof the fix holds), plus the 2026-09-14 coverage-layer audit's
regression fixtures (coverage-metric reconciliation, RotoWire player-page
lookup, source_attempted/entity_matched/signal_found separation).

Run with: python -m unittest test_soccer_adapter -v

Isolation contract (coverage audit item 9): NOTHING in this file may hit
a real network call or read/write a real production file under data/ -
every external/cache call (ESPN, Transfermarkt, RotoWire feed, RotoWire
player-page index/status, X) is mocked or fed an explicit empty/fixture
context. _empty_context() sets rotowire_use_disk_cache=False and an empty
rotowire_player_index specifically so rotowire_soccer.get_player_status
can never touch data/rotowire_soccer_player_status_cache.json; every
Transfermarkt-touching test patches soccer_adapter.get_team_injuries_
cached directly rather than letting _enrich_transfermarkt reach the real
scrapers.transfermarkt module (which would touch data/transfermarkt/...).
"""

import unittest
from unittest.mock import patch

import soccer_adapter
from scrapers.betr import normalize_name

NOT_YET_POSTED = ("not_yet_posted", None,
                   {"source_attempted": False, "entity_matched": False, "signal_found": False})


def _empty_context(**overrides):
    context = {
        "rotowire": {}, "rotowire_player_index": {}, "rotowire_status_cache": {},
        "rotowire_use_disk_cache": False, "history": {}, "espn_team_cache": {}, "espn_event_cache": {},
        "transfermarkt_cache": {}, "dabble_live_players": [],
    }
    context.update(overrides)
    return context


def _no_transfermarkt():
    """Patches the real network/disk-cache entry point Transfermarkt
    enrichment calls, so any test not specifically exercising Transfermarkt
    gets a deterministic empty result with zero real I/O."""
    return patch("soccer_adapter.get_team_injuries_cached", return_value=[])


class TestNameNormalizationRegressionFixtures(unittest.TestCase):
    """Item 2 - Dedic/Balde must resolve to the same player regardless of
    which spelling a source uses."""

    def test_dedic_diacritic_and_ascii_match(self):
        self.assertEqual(normalize_name("Amar Dedić"), normalize_name("Amar Dedic"))

    def test_balde_diacritic_and_ascii_match(self):
        self.assertEqual(normalize_name("Mama Baldé"), normalize_name("Mama Balde"))

    def test_dedic_groups_together_across_prop_spellings(self):
        board = {"sport": "soccer", "props": [
            {"player_name": "Amar Dedić", "team": "NEW", "market": "Assists", "line": 0.5,
             "event_date": "2026-09-14T19:00:00.000Z", "matchup": "Newcastle United @ Leeds United",
             "league": "England - Premier League", "position": "DF"},
            {"player_name": "Amar Dedic", "team": "NEW", "market": "Shots", "line": 0.5,
             "event_date": "2026-09-14T19:00:00.000Z", "matchup": "Newcastle United @ Leeds United",
             "league": "England - Premier League", "position": "DF"},
        ]}
        props = soccer_adapter.load_dabble_soccer_props(board)
        players = soccer_adapter.group_props_by_player(props)
        self.assertEqual(len(players), 1, "diacritic vs. ascii spelling of Dedic must group as one player")
        (_, entry), = players.items()
        self.assertEqual(len(entry["props"]), 2)


class TestDedicFixture(unittest.TestCase):
    """Proves the actual root-cause fix: a live Dabble player with a real
    injury/status signal from an external source still gets scored, with
    that signal visible - Newcastle fixture resolves, GTD-style info
    enriches him, live Dabble prop keeps him in DNS."""

    def _dedic_player(self):
        props = soccer_adapter.load_dabble_soccer_props({"sport": "soccer", "props": [
            {"player_name": "Amar Dedić", "team": "NEW", "market": "Assists", "line": 0.5,
             "event_date": "2026-09-14T19:00:00.000Z", "matchup": "Newcastle United @ Leeds United",
             "league": "England - Premier League", "position": "DF", "player_id": "espn-dedic-1"},
        ]})
        return list(soccer_adapter.group_props_by_player(props).values())[0]

    def test_newcastle_fixture_resolves_and_player_stays_a_candidate(self):
        player = self._dedic_player()
        context = _empty_context()
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertEqual(result["player_name"], "Amar Dedić")
        self.assertEqual(result["league_code"], "EPL")
        self.assertIsInstance(result["dns_score"], int)

    def test_gtd_style_signal_enriches_and_raises_score(self):
        player = self._dedic_player()
        context = _empty_context(rotowire={
            "amar dedic": {
                "rotowire_status_raw": "fitness-test", "rotowire_status_normalized": "soft",
                "rotowire_injury": "Amar Dedic: Doubtful with hamstring issue",
                "rotowire_news_at": "2026-09-14T08:00:00+00:00", "rotowire_predicted_start": None,
            }
        })
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertIsNotNone(result["rotowire_status_raw"])
        self.assertTrue(any("RotoWire" in label for label in result["top_reasons"]),
                         "the RotoWire GTD-style signal must be visible in the reasons")
        self.assertGreater(result["dns_score"], soccer_adapter.soccer_dns_score.BASE_SCORE)

    def test_live_dabble_prop_alone_is_sufficient_to_be_a_candidate(self):
        """The core architecture rule: zero enrichment from any external
        source must still produce a scored candidate, never a dropped one."""
        player = self._dedic_player()
        context = _empty_context()
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertIsInstance(result["dns_score"], int)
        self.assertTrue(result["low_data"], "with zero enrichment this must be flagged LOW DATA, not dropped")
        self.assertEqual(result["watchdog_note"], "LOW DATA / ENRICHMENT MISSING")
        self.assertTrue(result["board_only"], "zero external signal from any of the 4 raw sources -> board_only")
        self.assertFalse(result["has_any_external_enrichment"])

    def test_player_page_gtd_signal_used_when_feed_has_nothing(self):
        """2026-09-14 coverage audit's central finding: RotoWire's own
        player PAGE (not just the daily headline feed) showed Amar Dedic
        as GTD/hamstring even on a day with no fresh headline about him -
        the exact signal the Dabble UI itself was displaying that the
        feed-only path had been missing entirely (item 2/3)."""
        player = self._dedic_player()
        context = _empty_context(rotowire={})  # feed has nothing today
        page_status = {
            "attempted": True, "matched": True, "ambiguous": False,
            "page_url": "https://www.rotowire.com/soccer/player/amar-dedic-33528", "player_id": "33528",
            "status_tag": "GTD", "rotowire_status_normalized": "fifty-fifty", "injury": "Hamstring",
            "est_return": "9/14/2026", "signal_found": True, "source": "player_page",
            "checked_at": "2026-09-14T08:00:00+00:00",
        }
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), \
             _no_transfermarkt(), \
             patch("rotowire_soccer.get_player_status", return_value=page_status):
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertEqual(result["rotowire_page_tag"], "GTD", "the literal page tag must be visible for display")
        self.assertEqual(result["rotowire_status_raw"], "fifty-fifty",
                         "scoring must see the MAPPED tier, not the literal page tag - "
                         "a bug found live 2026-09-14 put 'GTD' here directly, which silently "
                         "scored 0 since it isn't a ROTOWIRE_STATUS_PRIOR key")
        self.assertEqual(result["rotowire_source"], "player_page")
        self.assertTrue(result["has_news"], "a player-page-only signal must still count as coverage")
        self.assertFalse(result["board_only"])
        self.assertGreater(result["dns_score"], soccer_adapter.soccer_dns_score.BASE_SCORE,
                            "the mapped GTD signal must actually move the score, not silently no-op")


class TestBaldeFixture(unittest.TestCase):
    """Proves Ligue 1 coverage, recent-start/rotation signals, and that
    disagreement between Dabble status and a predicted-XI-style source is
    preserved rather than one overwriting the other - item 4/12."""

    def _balde_player(self):
        props = soccer_adapter.load_dabble_soccer_props({"sport": "soccer", "props": [
            {"player_name": "Mama Baldé", "team": "BRE", "market": "Shots", "line": 0.5,
             "event_date": "2026-09-14T19:00:00.000Z", "matchup": "Paris Saint-Germain @ Stade Brestois 29",
             "league": "France - Ligue 1", "position": "FW", "player_id": "espn-balde-1"},
        ]})
        return list(soccer_adapter.group_props_by_player(props).values())[0]

    def test_ligue1_league_code_resolves(self):
        player = self._balde_player()
        self.assertEqual(soccer_adapter._league_code(player["league_raw"]), "L1F")

    def test_recent_start_pattern_used_when_available(self):
        player = self._balde_player()
        history = {"mama balde": {"name": "mama balde", "matches": [
            {"date": "2026-08-20", "league": "L1F", "event_id": "1", "team_id": "t1",
             "opponent_team_id": "t2", "started": False, "active": True},
            {"date": "2026-08-27", "league": "L1F", "event_id": "2", "team_id": "t1",
             "opponent_team_id": "t3", "started": False, "active": False},
            {"date": "2026-09-03", "league": "L1F", "event_id": "3", "team_id": "t1",
             "opponent_team_id": "t4", "started": True, "active": True},
        ]}}
        import soccer_start_history
        self.assertTrue(soccer_start_history.first_recent_start(history, "mama balde"),
                         "his first start of the season last match must register")

    def test_disagreement_between_dabble_status_and_predicted_xi_is_preserved(self):
        """Dabble/external status says doubt, recent starts say trending
        toward starting - BOTH must remain visible, neither erased."""
        player = self._balde_player()
        history = {"mama balde": {"name": "mama balde", "matches": [
            {"date": "2026-08-20", "league": "L1F", "event_id": "1", "team_id": "t1",
             "opponent_team_id": "t2", "started": False, "active": True},
            {"date": "2026-08-27", "league": "L1F", "event_id": "2", "team_id": "t1",
             "opponent_team_id": "t3", "started": False, "active": True},
            {"date": "2026-09-03", "league": "L1F", "event_id": "3", "team_id": "t1",
             "opponent_team_id": "t4", "started": True, "active": True},
        ]}}
        context = _empty_context(
            history=history,
            rotowire={"mama balde": {
                "rotowire_status_raw": "fitness-test", "rotowire_status_normalized": "soft",
                "rotowire_injury": "Balde a doubt", "rotowire_news_at": None, "rotowire_predicted_start": None,
            }},
        )
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)

        self.assertTrue(result["predicted_bench_sources"], "the RotoWire doubt signal must appear as a bench source")
        self.assertTrue(result["predicted_start_sources"],
                         "the recent-start-pattern signal must ALSO appear, not be overwritten")
        self.assertTrue(result["prediction_disagreement"])
        self.assertEqual(result["prediction_consensus"], "disagreement")
        # Both directions must show up in the itemized reasons, not just the net score.
        joined_reasons = " | ".join(result["top_reasons"])
        self.assertIn("RotoWire", joined_reasons)
        self.assertTrue(any("Predicted starter" in r for r in result["top_reasons"]))
        self.assertTrue(any("Predicted bench" in r for r in result["top_reasons"]))


class TestNeverDropOnEnrichmentFailure(unittest.TestCase):
    """Item 10 - a Dabble player must survive scoring even when every
    single enrichment source fails/errors, marked LOW DATA, never dropped."""

    def test_all_enrichment_sources_failing_still_scores(self):
        props = soccer_adapter.load_dabble_soccer_props({"sport": "soccer", "props": [
            {"player_name": "Some Obscure Player", "team": "XYZ", "market": "Shots", "line": 0.5,
             "event_date": "2026-09-14T19:00:00.000Z", "matchup": "XYZ @ ABC",
             "league": "Unsupported League Nobody Maps", "position": "MF"},
        ]})
        player = list(soccer_adapter.group_props_by_player(props).values())[0]
        context = _empty_context()
        with _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertIsInstance(result["dns_score"], int)
        self.assertTrue(result["low_data"])
        self.assertIsNone(result["league_code"], "an unmapped league must not crash - just no ESPN/history enrichment")


class TestCoverageMetricsReconciliation(unittest.TestCase):
    """2026-09-14 coverage audit item 1 - the original coverage_report had
    predicted_lineup_coverage=3/76 alongside completely_unenriched=76/76,
    which is contradictory under any intuitive reading: a player counted
    toward predicted-lineup coverage necessarily has AT LEAST ONE real
    signal backing it, so he cannot also be "completely unenriched." These
    tests pin down the explicit, non-overlapping definitions that fix that."""

    def _player(self, **overrides):
        base = {"player_name": "Coverage Test Player", "team": "NEW", "market": "Shots", "line": 0.5,
                "event_date": "2026-09-14T19:00:00.000Z", "matchup": "NEW @ LEE",
                "league": "England - Premier League", "position": "MF"}
        base.update(overrides)
        props = soccer_adapter.load_dabble_soccer_props({"sport": "soccer", "props": [base]})
        return list(soccer_adapter.group_props_by_player(props).values())[0]

    def test_zero_signals_is_board_only_and_completely_unenriched(self):
        player = self._player()
        context = _empty_context()
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertTrue(result["board_only"])
        self.assertFalse(result["has_history"])
        self.assertFalse(result["has_injury"])
        self.assertFalse(result["has_news"])
        self.assertFalse(result["has_x_signal"])
        self.assertFalse(result["has_any_external_enrichment"])
        self.assertFalse(result["has_multiple_external_sources"])

    def test_one_source_is_not_board_only_and_not_multi_source(self):
        player = self._player()
        context = _empty_context(rotowire={"coverage test player": {
            "rotowire_status_raw": "fitness-test", "rotowire_status_normalized": "soft",
            "rotowire_injury": "doubt", "rotowire_news_at": None, "rotowire_predicted_start": None,
        }})
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertFalse(result["board_only"])
        self.assertTrue(result["has_news"])
        self.assertTrue(result["has_any_external_enrichment"])
        self.assertFalse(result["has_multiple_external_sources"], "exactly one raw source must not count as multi")

    def test_predicted_xi_coverage_implies_at_least_one_raw_source_never_board_only(self):
        """The exact contradiction this audit fixes: a candidate can never
        show predicted-XI coverage while ALSO being counted board_only -
        predicted_xi_sources is built FROM the raw sources, so if it's
        non-empty, has_any_external_enrichment must be True."""
        player = self._player()
        history = {"coverage test player": {"name": "coverage test player", "matches": [
            {"date": "2026-08-01", "league": "EPL", "event_id": "1", "team_id": "t1",
             "opponent_team_id": "t2", "started": True, "active": True},
            {"date": "2026-08-08", "league": "EPL", "event_id": "2", "team_id": "t1",
             "opponent_team_id": "t3", "started": True, "active": True},
            {"date": "2026-08-15", "league": "EPL", "event_id": "3", "team_id": "t1",
             "opponent_team_id": "t4", "started": True, "active": True},
        ]}}
        # espn_team_cache pre-seeded with the SAME "t1" placeholder the
        # history fixture above uses - team_starts_last_n (2026-09-16)
        # needs a real, matching team id to find team fixtures at all, so
        # a mismatch (e.g. the real ESPN id a live lookup would return)
        # would silently find nothing - this keeps the test deterministic
        # and network-free rather than depending on a live ESPN call.
        context = _empty_context(history=history, espn_team_cache={("EPL", "NEW"): "t1"})
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertTrue(result["has_predicted_xi"])
        self.assertTrue(result["has_history"])
        self.assertFalse(result["board_only"])
        self.assertTrue(result["has_any_external_enrichment"])

    def test_two_independent_sources_counts_as_multi_source(self):
        player = self._player()
        history = {"coverage test player": {"name": "coverage test player", "matches": [
            {"date": "2026-08-01", "league": "EPL", "event_id": "1", "team_id": "t1",
             "opponent_team_id": "t2", "started": True, "active": True},
            {"date": "2026-08-08", "league": "EPL", "event_id": "2", "team_id": "t1",
             "opponent_team_id": "t3", "started": True, "active": True},
            {"date": "2026-08-15", "league": "EPL", "event_id": "3", "team_id": "t1",
             "opponent_team_id": "t4", "started": True, "active": True},
        ]}}
        context = _empty_context(history=history, espn_team_cache={("EPL", "NEW"): "t1"},
                                  rotowire={"coverage test player": {
            "rotowire_status_raw": "fitness-test", "rotowire_status_normalized": "soft",
            "rotowire_injury": "doubt", "rotowire_news_at": None, "rotowire_predicted_start": None,
        }})
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertTrue(result["has_multiple_external_sources"])

    def test_coverage_report_bucket_counts_reconcile_to_total(self):
        """The exact print contract item 1 asks for: every candidate falls
        into EXACTLY one of board-only / one-source / two-plus-source, and
        those three counts must sum to the total - no double counting, no
        gaps."""
        players = []
        contexts = []
        # 0 sources
        p0 = self._player(player_name="Zero Src")
        players.append(p0)
        contexts.append(_empty_context())
        # 1 source (news)
        p1 = self._player(player_name="One Src")
        players.append(p1)
        contexts.append(_empty_context(rotowire={"one src": {
            "rotowire_status_raw": "fitness-test", "rotowire_status_normalized": "soft",
            "rotowire_injury": "doubt", "rotowire_news_at": None, "rotowire_predicted_start": None}}))
        # 2 sources (news + history)
        p2 = self._player(player_name="Two Src")
        hist = {"two src": {"name": "two src", "matches": [
            {"date": "2026-08-01", "league": "EPL", "event_id": "1", "team_id": "t1",
             "opponent_team_id": "t2", "started": True, "active": True}] * 1}}
        contexts.append(_empty_context(history=hist, espn_team_cache={("EPL", "NEW"): "t1"},
                                        rotowire={"two src": {
            "rotowire_status_raw": "fitness-test", "rotowire_status_normalized": "soft",
            "rotowire_injury": "doubt", "rotowire_news_at": None, "rotowire_predicted_start": None}}))
        players.append(p2)

        results = []
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            for player, ctx in zip(players, contexts):
                results.append(soccer_adapter.enrich_and_score_player(player, ctx))

        board_only = sum(1 for r in results if r["board_only"])
        one_source = sum(1 for r in results if r["has_any_external_enrichment"] and not r["has_multiple_external_sources"])
        two_plus = sum(1 for r in results if r["has_multiple_external_sources"])
        self.assertEqual(board_only + one_source + two_plus, len(results))
        self.assertEqual(board_only, 1)
        self.assertEqual(one_source, 1)
        self.assertEqual(two_plus, 1)


class TestSourceAttemptedVsMatchedVsSignalFound(unittest.TestCase):
    """Item 6 - "source available" (attempted), "found the right entity"
    (matched), and "found something adverse" (signal_found) are three
    different facts and must never be collapsed into one number."""

    def _player(self):
        props = soccer_adapter.load_dabble_soccer_props({"sport": "soccer", "props": [
            {"player_name": "Health Check Player", "team": "NEW", "market": "Shots", "line": 0.5,
             "event_date": "2026-09-14T19:00:00.000Z", "matchup": "NEW @ LEE",
             "league": "England - Premier League", "position": "MF"},
        ]})
        return list(soccer_adapter.group_props_by_player(props).values())[0]

    def test_transfermarkt_team_matched_but_no_injury_is_a_real_healthy_result(self):
        player = self._player()
        context = _empty_context()
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), \
             patch("soccer_adapter.get_team_injuries_cached", return_value=[]), \
             patch("soccer_adapter.resolve_team_by_code", return_value=("361", "Newcastle United")):
            result = soccer_adapter.enrich_and_score_player(player, context)
        health = result["source_health"]["transfermarkt"]
        self.assertTrue(health["source_attempted"])
        self.assertTrue(health["entity_matched"], "team resolved to a real club")
        self.assertFalse(health["signal_found"], "checked and genuinely healthy - not a gap")

    def test_transfermarkt_team_unresolved_is_a_real_gap_not_healthy(self):
        player = self._player()
        context = _empty_context()
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), \
             patch("soccer_adapter.get_team_injuries_cached", return_value=[]), \
             patch("soccer_adapter.resolve_team_by_code", return_value=None):
            result = soccer_adapter.enrich_and_score_player(player, context)
        health = result["source_health"]["transfermarkt"]
        self.assertTrue(health["source_attempted"])
        self.assertFalse(health["entity_matched"], "team never resolved - a real coverage gap")
        self.assertFalse(health["signal_found"])

    def test_x_not_attempted_when_no_credentials(self):
        player = self._player()
        context = _empty_context()
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), \
             _no_transfermarkt(), \
             patch("soccer_x_monitor.has_credentials", return_value=False):
            result = soccer_adapter.enrich_and_score_player(player, context)
        health = result["source_health"]["x"]
        self.assertFalse(health["source_attempted"], "must read as DISABLED, never as a checked-and-empty result")
        self.assertFalse(health["entity_matched"])
        self.assertFalse(health["signal_found"])

    def test_x_attempted_and_matched_when_credentials_present_and_hit_found(self):
        player = self._player()
        context = _empty_context()
        fake_hit = {"extracted_injury": "out", "extracted_start_signal": None, "author_username": "beat",
                    "author_trust_tier": 2, "post_id": "1", "created_at": "2026-09-14T00:00:00Z"}
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), \
             _no_transfermarkt(), \
             patch("soccer_x_monitor.has_credentials", return_value=True), \
             patch("soccer_x_monitor.get_player_x_signal", return_value=fake_hit):
            result = soccer_adapter.enrich_and_score_player(player, context)
        health = result["source_health"]["x"]
        self.assertTrue(health["source_attempted"])
        self.assertTrue(health["entity_matched"])
        self.assertTrue(health["signal_found"])

    def test_rotowire_player_page_matched_tracked_separately_from_signal(self):
        """70/76 resolving to a real RotoWire page with only 2 adverse
        hits is GOOD coverage; that distinction only exists if match rate
        and signal rate are tracked separately - item 3."""
        player = self._player()
        context = _empty_context()
        healthy_page = {
            "attempted": True, "matched": True, "ambiguous": False,
            "page_url": "https://www.rotowire.com/soccer/player/health-check-player-1", "player_id": "1",
            "status_tag": None, "rotowire_status_normalized": None, "injury": None, "est_return": None,
            "signal_found": False, "source": "player_page", "checked_at": "2026-09-14T08:00:00+00:00",
        }
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), \
             _no_transfermarkt(), \
             patch("rotowire_soccer.get_player_status", return_value=healthy_page):
            result = soccer_adapter.enrich_and_score_player(player, context)
        health = result["source_health"]["rotowire"]
        self.assertTrue(health["entity_matched"], "matched a real RotoWire page")
        self.assertFalse(health["signal_found"], "but nothing adverse on it - a real healthy result")
        self.assertFalse(result["has_news"])


class TestPredictedXiVoteStructure(unittest.TestCase):
    """2026-09-14 item 4 - starter_votes/bench_votes/unknown_votes/
    predicted_xi_source_count must never let one source overwrite
    another, and a checked-but-non-directional source must show up as
    an "unknown" vote rather than being silently dropped."""

    def _player(self):
        props = soccer_adapter.load_dabble_soccer_props({"sport": "soccer", "props": [
            {"player_name": "Vote Test Player", "team": "NEW", "market": "Shots", "line": 0.5,
             "event_date": "2026-09-14T19:00:00.000Z", "matchup": "NEW @ LEE",
             "league": "England - Premier League", "position": "MF"},
        ]})
        return list(soccer_adapter.group_props_by_player(props).values())[0]

    def test_ambiguous_rotowire_status_counted_as_unknown_not_dropped(self):
        player = self._player()
        context = _empty_context(rotowire={"vote test player": {
            "rotowire_status_raw": "monitored", "rotowire_status_normalized": "monitored",
            "rotowire_injury": "being monitored", "rotowire_news_at": None, "rotowire_predicted_start": None,
        }})
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertEqual(result["starter_votes"], 0)
        self.assertEqual(result["bench_votes"], 0)
        self.assertEqual(result["unknown_votes"], 1)
        self.assertEqual(result["predicted_xi_source_count"], 1)
        self.assertIn("rotowire_news", result["predicted_unknown_sources"])

    def test_votes_never_overwrite_each_other_multiple_sources(self):
        player = self._player()
        history = {"vote test player": {"name": "vote test player", "matches": [
            {"date": "2026-08-01", "league": "EPL", "event_id": "1", "team_id": "t1",
             "opponent_team_id": "t2", "started": True, "active": True},
            {"date": "2026-08-08", "league": "EPL", "event_id": "2", "team_id": "t1",
             "opponent_team_id": "t3", "started": True, "active": True},
            {"date": "2026-08-15", "league": "EPL", "event_id": "3", "team_id": "t1",
             "opponent_team_id": "t4", "started": True, "active": True},
        ]}}
        context = _empty_context(history=history, espn_team_cache={("EPL", "NEW"): "t1"},
                                  rotowire={"vote test player": {
            "rotowire_status_raw": "fitness-test", "rotowire_status_normalized": "soft",
            "rotowire_injury": "doubt", "rotowire_news_at": None, "rotowire_predicted_start": None}})
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)
        # history says start (3/3), rotowire says bench - BOTH must be
        # counted, neither one clobbering the other.
        self.assertEqual(result["starter_votes"], 1)
        self.assertEqual(result["bench_votes"], 1)
        self.assertEqual(result["predicted_xi_source_count"], 2)
        self.assertEqual(result["prediction_consensus"], "disagreement")

    def test_zero_sources_all_vote_counts_zero(self):
        player = self._player()
        context = _empty_context()
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), _no_transfermarkt():
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertEqual((result["starter_votes"], result["bench_votes"], result["unknown_votes"]), (0, 0, 0))
        self.assertEqual(result["predicted_xi_source_count"], 0)


class TestHardOutFloorIntegration(unittest.TestCase):
    """2026-09-16 Hinshelwood item 2 - the full enrich_and_score_player
    path from a RotoWire hard_out tag through to the floored dns_score
    and the corroboration/conflict flags surfaced on the candidate dict."""

    def _hinshelwood_player(self):
        props = soccer_adapter.load_dabble_soccer_props({"sport": "soccer", "props": [
            {"player_name": "Jack Hinshelwood", "team": "NEW", "market": "Shots", "line": 0.5,
             "event_date": "2026-09-16T19:00:00.000Z", "matchup": "Newcastle United @ Leeds United",
             "league": "England - Premier League", "position": "MF", "player_id": "espn-hinshelwood-1"},
        ]})
        return list(soccer_adapter.group_props_by_player(props).values())[0]

    def _hard_out_page(self, status_since="2026-09-10T00:00:00+00:00"):
        return {
            "attempted": True, "matched": True, "ambiguous": False,
            "page_url": "https://www.rotowire.com/soccer/player/jack-hinshelwood-1", "player_id": "1",
            "status_tag": "Out", "rotowire_status_normalized": "hard_out", "injury": "Hip",
            "est_return": "TBD", "signal_found": True, "source": "player_page",
            "checked_at": "2026-09-16T08:00:00+00:00", "status_since": status_since,
        }

    def _absent_history(self):
        """The team's most recent fixture (e10) has no entry at all for
        this player - a teammate's entry is what proves the fixture
        happened. No entry anywhere shows a start after status_since."""
        return {
            "jack hinshelwood": {"name": "jack hinshelwood", "matches": [
                {"date": "2026-08-23", "league": "EPL", "event_id": "e9", "team_id": "t1",
                 "opponent_team_id": "opp", "competition": "EPL", "home_away": "home",
                 "started": True, "active": True},
            ]},
            "teammate": {"name": "teammate", "matches": [
                {"date": "2026-08-23", "league": "EPL", "event_id": "e9", "team_id": "t1",
                 "opponent_team_id": "opp", "competition": "EPL", "home_away": "home",
                 "started": True, "active": True},
                {"date": "2026-09-13", "league": "EPL", "event_id": "e10", "team_id": "t1",
                 "opponent_team_id": "opp", "competition": "EPL", "home_away": "home",
                 "started": True, "active": True},
            ]},
        }

    def test_hard_out_alone_floors_the_score_with_no_corroboration_flagged(self):
        player = self._hinshelwood_player()
        context = _empty_context(rotowire={}, espn_team_cache={("EPL", "NEW"): "t1"})
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), \
             _no_transfermarkt(), \
             patch("rotowire_soccer.get_player_status", return_value=self._hard_out_page()):
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertGreaterEqual(result["dns_score"], soccer_adapter.soccer_dns_score.HARD_OUT_BASE_FLOOR)
        self.assertFalse(result["hard_out_corroborated"])
        self.assertIsNone(result["hard_out_conflict"])

    def test_hard_out_corroborated_by_transfermarkt_raises_the_floor(self):
        player = self._hinshelwood_player()
        context = _empty_context(rotowire={}, espn_team_cache={("EPL", "NEW"): "t1"})
        tm_hit = [{"normalized_name": player["normalized_name"], "section": "Injuries",
                   "reason": "Hip injury", "since": "Aug 26, 2026", "expected_return": None,
                   "expected_return_date": None}]
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), \
             patch("soccer_adapter.get_team_injuries_cached", return_value=tm_hit), \
             patch("rotowire_soccer.get_player_status", return_value=self._hard_out_page()):
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertTrue(result["hard_out_corroborated"])
        self.assertIn("Transfermarkt injury list", result["hard_out_corroboration_sources"])
        self.assertGreaterEqual(result["dns_score"], soccer_adapter.soccer_dns_score.HARD_OUT_CORROBORATED_FLOOR)

    def test_hard_out_corroborated_by_absence_from_most_recent_squad(self):
        player = self._hinshelwood_player()
        context = _empty_context(rotowire={}, espn_team_cache={("EPL", "NEW"): "t1"},
                                  history=self._absent_history())
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), \
             _no_transfermarkt(), \
             patch("rotowire_soccer.get_player_status", return_value=self._hard_out_page()):
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertTrue(result["hard_out_corroborated"])
        self.assertIn("absent from most recent squad", result["hard_out_corroboration_sources"])

    def test_hard_out_conflict_when_player_started_after_the_tag_was_first_seen(self):
        """The tag was first seen 2026-09-10, but history shows a start
        on 2026-09-13 (AFTER that) - the floor must not be trusted, and
        the conflict must be surfaced rather than silently ignored."""
        history = self._absent_history()
        history["jack hinshelwood"]["matches"].append(
            {"date": "2026-09-13", "league": "EPL", "event_id": "e10", "team_id": "t1",
             "opponent_team_id": "opp", "competition": "EPL", "home_away": "home",
             "started": True, "active": True})
        player = self._hinshelwood_player()
        context = _empty_context(rotowire={}, espn_team_cache={("EPL", "NEW"): "t1"}, history=history)
        with patch("soccer_adapter._enrich_espn_official_status", return_value=NOT_YET_POSTED), \
             _no_transfermarkt(), \
             patch("rotowire_soccer.get_player_status", return_value=self._hard_out_page()):
            result = soccer_adapter.enrich_and_score_player(player, context)
        self.assertIsNotNone(result["hard_out_conflict"])
        self.assertIn("2026-09-13", result["hard_out_conflict"])
        self.assertLess(result["dns_score"], soccer_adapter.soccer_dns_score.HARD_OUT_BASE_FLOOR)


if __name__ == "__main__":
    unittest.main()
