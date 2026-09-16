"""
Tests for soccer_dns_score.py's RotoWire hard_out score floor (2026-09-16,
Hinshelwood item 2) - found live: Jack Hinshelwood (RotoWire hard_out,
Brighton, injured since 2026-08-26) still only scored 55, because the
+50 hard_out weight is just one additive contribution among several and
a thin/stale history signal can drag the additive total well below what
a specialist site confidently calling a player OUT should ever allow.
"""

import unittest

import soccer_dns_score as dns


def _base_kwargs(**overrides):
    kwargs = dict(
        transfermarkt_hit=None, rotowire_hit=None, x_hit=None,
        starts_last_5=(0, 0, 0), starts_last_10=(0, 0, 0),
        first_recent_start=False, frequent_substitute=False, rotation_player=False,
        predicted_start_sources=[], predicted_bench_sources=[], event_date=None,
    )
    kwargs.update(overrides)
    return kwargs


class TestHardOutFloor(unittest.TestCase):
    def test_hard_out_with_no_corroboration_raises_score_to_base_floor(self):
        # Additive alone would be BASE_SCORE(3) + hard_out(50) = 53 - well
        # under the floor, reproducing the Hinshelwood 55-ish undershoot.
        score, contributions = dns._score_dns(
            **_base_kwargs(rotowire_hit={"rotowire_status_raw": "hard_out"}))
        self.assertEqual(score, dns.HARD_OUT_BASE_FLOOR)
        self.assertTrue(any("floor" in label.lower() for label, _ in contributions))

    def test_hard_out_corroborated_raises_score_higher_than_base_floor(self):
        score, _ = dns._score_dns(
            **_base_kwargs(rotowire_hit={"rotowire_status_raw": "hard_out"},
                            hard_out_corroborated=True))
        self.assertEqual(score, dns.HARD_OUT_CORROBORATED_FLOOR)
        self.assertGreater(dns.HARD_OUT_CORROBORATED_FLOOR, dns.HARD_OUT_BASE_FLOOR)

    def test_hard_out_conflict_suppresses_the_floor_entirely(self):
        # Player started the team's most recent match AFTER the hard_out
        # tag was first observed - don't trust the floor, flag instead.
        score, contributions = dns._score_dns(
            **_base_kwargs(rotowire_hit={"rotowire_status_raw": "hard_out"},
                            hard_out_conflict="started 2026-09-14 after hard_out first seen 2026-09-10"))
        self.assertEqual(score, 53)  # plain additive total, floor not applied
        self.assertTrue(any("conflict" in label.lower() for label, _ in contributions))

    def test_floor_never_lowers_a_score_that_is_already_above_it(self):
        # A hard_out tag PLUS strong corroborating history evidence can
        # already clear the floor on its own - the floor must never pull
        # a legitimately higher score back down.
        score, _ = dns._score_dns(
            **_base_kwargs(rotowire_hit={"rotowire_status_raw": "hard_out"},
                            starts_last_5=(0, 5, 5), hard_out_corroborated=True))
        self.assertGreaterEqual(score, dns.HARD_OUT_CORROBORATED_FLOOR)

    def test_non_hard_out_rotowire_tags_are_unaffected_by_the_floor(self):
        score, contributions = dns._score_dns(
            **_base_kwargs(rotowire_hit={"rotowire_status_raw": "fifty-fifty"}))
        self.assertEqual(score, 3 + dns.ROTOWIRE_STATUS_PRIOR["fifty-fifty"])
        self.assertFalse(any("floor" in label.lower() for label, _ in contributions))

    def test_score_candidate_passes_hard_out_inputs_through_to_the_floor(self):
        dns_score, confidence, urgency, evidence_count, contributions, urgency_reasons = dns.score_candidate(
            official_status="not_yet_posted",
            **_base_kwargs(rotowire_hit={"rotowire_status_raw": "hard_out"}))
        self.assertEqual(dns_score, dns.HARD_OUT_BASE_FLOOR)


class TestLockAwareSortKey(unittest.TestCase):
    """2026-09-17 item 1 - LOCK candidates (confirmed_not_starting or an
    uncontradicted RotoWire hard_out) rank above every additive-score
    candidate, regardless of dns_score/combined_priority. Ordering
    within LOCK: confirmed_not_starting, then corroborated hard_out,
    then uncorroborated hard_out, then soonest kickoff."""

    def _c(self, name, is_lock=False, lock_reason=None, combined_priority=0, event_date=None):
        return {"player_name": name, "is_lock": is_lock, "lock_reason": lock_reason,
                "combined_priority": combined_priority, "event_date": event_date}

    def test_any_lock_outranks_every_non_lock_regardless_of_priority(self):
        low_lock = self._c("Locked", is_lock=True, lock_reason="hard_out_uncorroborated", combined_priority=1)
        high_score = self._c("HighScore", is_lock=False, combined_priority=99)
        ranked = sorted([high_score, low_lock], key=dns.lock_aware_sort_key)
        self.assertEqual([c["player_name"] for c in ranked], ["Locked", "HighScore"])

    def test_lock_ordering_confirmed_then_corroborated_then_uncorroborated(self):
        confirmed = self._c("Confirmed", is_lock=True, lock_reason="confirmed_not_starting")
        corroborated = self._c("Corroborated", is_lock=True, lock_reason="hard_out_corroborated")
        uncorroborated = self._c("Uncorroborated", is_lock=True, lock_reason="hard_out_uncorroborated")
        ranked = sorted([uncorroborated, confirmed, corroborated], key=dns.lock_aware_sort_key)
        self.assertEqual([c["player_name"] for c in ranked], ["Confirmed", "Corroborated", "Uncorroborated"])

    def test_lock_tiebreak_is_soonest_kickoff(self):
        later = self._c("Later", is_lock=True, lock_reason="hard_out_uncorroborated",
                         event_date="2026-09-17T20:00:00.000Z")
        sooner = self._c("Sooner", is_lock=True, lock_reason="hard_out_uncorroborated",
                          event_date="2026-09-16T19:00:00.000Z")
        ranked = sorted([later, sooner], key=dns.lock_aware_sort_key)
        self.assertEqual([c["player_name"] for c in ranked], ["Sooner", "Later"])

    def test_non_lock_candidates_still_sort_by_combined_priority_descending(self):
        low = self._c("Low", combined_priority=10)
        high = self._c("High", combined_priority=90)
        ranked = sorted([low, high], key=dns.lock_aware_sort_key)
        self.assertEqual([c["player_name"] for c in ranked], ["High", "Low"])

    def test_lock_candidate_with_missing_kickoff_sorts_after_ones_with_a_real_kickoff(self):
        no_kickoff = self._c("NoKickoff", is_lock=True, lock_reason="hard_out_uncorroborated", event_date=None)
        with_kickoff = self._c("WithKickoff", is_lock=True, lock_reason="hard_out_uncorroborated",
                                event_date="2026-09-16T19:00:00.000Z")
        ranked = sorted([no_kickoff, with_kickoff], key=dns.lock_aware_sort_key)
        self.assertEqual([c["player_name"] for c in ranked], ["WithKickoff", "NoKickoff"])


if __name__ == "__main__":
    unittest.main()
