"""Unit tests for dnp_score.py's Phase 2.1 rework: DNP/confidence/urgency
are three separate, orthogonal scores. Run with: python -m unittest test_dnp_score -v

No network access - start_history/news_risk are plain in-memory dicts.
"""

import unittest
from datetime import datetime, timedelta, timezone

import dnp_score

EMPTY_HISTORY = {}
EMPTY_NEWS = {}


def _history_with_games(player_id, games):
    """games: list of {"started": bool, "opp_pitcher_hand": "L"|"R"|None}."""
    return {str(player_id): {"name": "Test Player", "games": [
        {"date": f"2026-08-{i+1:02d}", "game_pk": 1000 + i, "team": "NYY", "opp_team": "BOS",
         "started": g["started"], "batting_order": 3 if g["started"] else None,
         "entered_as_sub": False, "opp_pitcher_id": 1, "opp_pitcher_hand": g.get("opp_pitcher_hand"),
         "position": "SS"}
        for i, g in enumerate(games)
    ]}}


class TestUrgencyIndependentOfDnpScore(unittest.TestCase):
    """Moving a game's start time must never change the DNP score - only
    urgency. This is the core guarantee of the Phase 2.1 split."""

    def test_game_time_does_not_change_dnp_score(self):
        history = _history_with_games(111, [{"started": True}] * 8 + [{"started": False}] * 2)
        dnp_7pm, _ = dnp_score.score_dnp(
            player_id=111, name="Test Player", position="SS", start_status="not_yet_posted",
            opp_pitcher_hand=None, history=history, news_risk=EMPTY_NEWS,
        )
        dnp_1pm, _ = dnp_score.score_dnp(
            player_id=111, name="Test Player", position="SS", start_status="not_yet_posted",
            opp_pitcher_hand=None, history=history, news_risk=EMPTY_NEWS,
        )
        self.assertEqual(dnp_7pm, dnp_1pm, "score_dnp takes no time-of-game input at all - must be identical")

    def test_game_time_does_change_urgency(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        game_7pm = (now + timedelta(hours=7)).isoformat()   # far from first pitch
        game_1pm = (now + timedelta(hours=1)).isoformat()   # first pitch imminent

        urgency_far, _ = dnp_score.score_urgency(start_status="not_yet_posted", game_start=game_7pm, now=now)
        urgency_near, _ = dnp_score.score_urgency(start_status="not_yet_posted", game_start=game_1pm, now=now)
        self.assertNotEqual(urgency_far, urgency_near, "urgency must respond to time-to-first-pitch")
        self.assertGreater(urgency_near, urgency_far, "closer first pitch should mean higher urgency")


class TestConfidenceCappedWithoutLineupSource(unittest.TestCase):
    def test_no_lineup_source_cannot_reach_100_confidence(self):
        # Maximize every OTHER signal: full history sample, opposing hand
        # known, news available, fresh data - only the lineup-posted
        # source is missing.
        confidence, evidence_count = dnp_score.score_confidence(
            start_status="not_yet_posted", recent_n=10, opp_hand_known=True,
            news_available=True, scraped_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertLess(confidence, 100, "a not-yet-posted candidate must never show 100 confidence")
        # Sanity: the achievable ceiling without a posted lineup is exactly
        # 100 - CONFIDENCE_LINEUP_POSTED.
        self.assertEqual(confidence, 100 - dnp_score.CONFIDENCE_LINEUP_POSTED)

    def test_posted_lineup_can_reach_100_confidence(self):
        confidence, _ = dnp_score.score_confidence(
            start_status="confirmed_starting", recent_n=10, opp_hand_known=True,
            news_available=True, scraped_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertEqual(confidence, 100)


class TestConfirmedStatusForcesExtremeDnpScore(unittest.TestCase):
    def test_confirmed_starter_near_zero(self):
        dnp, _ = dnp_score.score_dnp(
            player_id=111, name="Test Player", position="SS", start_status="confirmed_starting",
            opp_pitcher_hand="R", history=EMPTY_HISTORY, news_risk=EMPTY_NEWS,
        )
        self.assertLessEqual(dnp, 10, "a confirmed starter must score near 0 regardless of any other signal")

    def test_confirmed_non_starter_near_100(self):
        dnp, _ = dnp_score.score_dnp(
            player_id=111, name="Test Player", position="SS", start_status="confirmed_not_starting",
            opp_pitcher_hand="R", history=EMPTY_HISTORY, news_risk=EMPTY_NEWS,
        )
        self.assertGreaterEqual(dnp, 90, "a confirmed non-starter must score near 100")


class TestMissingEnrichmentLowersConfidenceOnly(unittest.TestCase):
    """Missing enrichment (no opposing-pitcher hand, no history, no news)
    must lower CONFIDENCE, not crash scoring, and must not silently invent
    a DNP-score penalty for the gap - see module docstring."""

    def test_missing_signals_reduce_confidence_but_scoring_still_completes(self):
        full_confidence, full_n = dnp_score.score_confidence(
            start_status="not_yet_posted", recent_n=10, opp_hand_known=True,
            news_available=True, scraped_at=datetime.now(timezone.utc).isoformat(),
        )
        sparse_confidence, sparse_n = dnp_score.score_confidence(
            start_status="not_yet_posted", recent_n=0, opp_hand_known=False,
            news_available=False, scraped_at=None,
        )
        self.assertLess(sparse_confidence, full_confidence)
        self.assertLess(sparse_n, full_n)

        # Scoring itself must still produce a valid result with nothing to
        # go on at all.
        dnp, contributions = dnp_score.score_dnp(
            player_id=999999, name="Nobody Known", position=None, start_status="not_yet_posted",
            opp_pitcher_hand=None, history=EMPTY_HISTORY, news_risk=EMPTY_NEWS,
        )
        self.assertIsInstance(dnp, int)
        self.assertGreaterEqual(dnp, 0)
        self.assertLessEqual(dnp, 100)
        self.assertTrue(len(contributions) >= 1)


if __name__ == "__main__":
    unittest.main()
