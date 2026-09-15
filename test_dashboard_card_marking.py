"""Regression tests for the 2026-09-14 "cards look pre-marked" bug.

Root cause: the diagonal-striped/dimmed ".pending-lineup" treatment (a
DATA-DRIVEN "lineup not confirmed yet" indicator) was a completely
different mechanism from the manual ".used"/"Clear all marks" click-to-
mark system, but visually the two were easy to confuse - a slate where
most players were incorrectly flagged lineup_confirmed=False (the Bug 1
stale-lineup issue) made most cards look dimmed+faded, which reads as
"already marked" even though nobody clicked anything.

That treatment was removed outright on 2026-09-15, together with the
confirmed-lineup hard gate in report.is_actionable, so ".used" is now the
only thing that dims a card and the confusion is gone by construction
rather than by correct data. The tests below pin both halves: the manual
mark system still behaves, and .pending-lineup stays gone.

This repo has no JS execution harness (no Node/browser test runner), so
the ".used"/localStorage logic is verified the honest way available here:
structural assertions against the ACTUAL generated report.py source/HTML
- the exact bytes that ship - rather than a live DOM. build_card/row-level
behavior is tested directly in Python.

Run with: python -m unittest test_dashboard_card_marking -v
"""

import re
import unittest

import report


class TestUsedMarkStorageKeyVersionedAndDateScoped(unittest.TestCase):
    """Item: "any persisted mark key must be scoped by slate_date +
    player_id, or use a date-versioned storage namespace" / "migrate/
    version it... mlb_marks_v2_2026-09-14 rather than one permanent
    global markedPlayers collection." """

    def setUp(self):
        with open("report.py", encoding="utf-8") as f:
            self.source = f.read()

    def test_storage_key_is_versioned_and_includes_game_date(self):
        self.assertIn("const usedStorageKey = 'mlb_marks_v2_' + GAME_DATE;", self.source)

    def test_no_bare_unscoped_global_marks_key(self):
        # The literal anti-pattern named in the request - must never exist.
        self.assertNotIn("markedPlayers", self.source)
        self.assertNotIn("localStorage.getItem('markedPlayers')", self.source)

    def test_legacy_unversioned_key_scheme_is_swept_on_load(self):
        self.assertIn("mlbUsedCards_", self.source, "the OLD key prefix must still be referenced - by the cleanup sweep")
        # It must appear inside a removal sweep, not a live read/write path.
        sweep_idx = self.source.find("localStorage.removeItem(k)")
        legacy_ref_idx = self.source.find("k.startsWith('mlbUsedCards_')")
        self.assertNotEqual(sweep_idx, -1)
        self.assertNotEqual(legacy_ref_idx, -1)
        self.assertLess(abs(sweep_idx - legacy_ref_idx), 200, "the legacy prefix check and its removal must be in the same sweep block")


class TestUsedMarkAppliedOnlyWhenTrue(unittest.TestCase):
    """Item: "verify that the striped/dim CSS class is only added when
    marked === true and is not accidentally the default card state" -
    for the REAL click-to-mark system (.used), the only dimming
    treatment left after .pending-lineup's removal."""

    def setUp(self):
        with open("report.py", encoding="utf-8") as f:
            self.source = f.read()

    def test_used_class_gated_by_set_membership_not_unconditional(self):
        self.assertIn("if (usedIds.has(c.playerId)) card.classList.add('used');", self.source)
        # Must never unconditionally add 'used' to a freshly-built card.
        self.assertNotIn("card.classList.add('used');\n", self.source.replace(
            "if (usedIds.has(c.playerId)) card.classList.add('used');\n", ""))

    def test_apply_used_state_toggles_based_on_membership_both_directions(self):
        self.assertIn(
            "el.classList.toggle('used', usedIds.has(Number(el.dataset.playerId)));", self.source,
            "toggle(bool) must be used (not .add) so a card correctly LOSES .used when its id isn't marked")

    def test_fresh_used_ids_set_starts_empty(self):
        self.assertIn("let usedIds = new Set();", self.source)

    def test_clear_all_marks_empties_the_set(self):
        self.assertIn("usedIds.clear();", self.source)


class TestPendingLineupTreatmentIsGone(unittest.TestCase):
    """.pending-lineup was removed 2026-09-15 along with the confirmed-
    lineup hard gate in report.is_actionable (an unconfirmed lineup no
    longer disqualifies a play, so dimming the whole card overstated it).
    It must not come back: the striped dimming is exactly what made a
    normal card read as "already clicked," and .used is now the only
    treatment that dims anything."""

    def setUp(self):
        with open("report.py", encoding="utf-8") as f:
            self.source = f.read()

    def test_render_card_never_applies_a_pending_lineup_class(self):
        self.assertIsNone(re.search(r"const pending\s*=", self.source),
                          "renderCard must not compute a pending-lineup class any more")
        self.assertNotIn("'pending-lineup'", self.source)
        self.assertNotIn('"pending-lineup"', self.source)

    def test_card_class_name_is_built_from_tier_and_treatment_only(self):
        self.assertIn("card.className = ('card ' + c.tier + ' ' + treatment).trim();", self.source)

    def test_pending_lineup_css_rule_is_removed(self):
        self.assertNotIn(".card.pending-lineup", self.source)

    def test_used_remains_the_only_dimming_treatment(self):
        self.assertIn(".card.used", self.source)
        used_block = self.source[self.source.index(".card.used {{"):][:120]
        self.assertNotIn("repeating-linear-gradient", used_block)


class TestIsActionableNoLongerGatesOnLineupConfirmation(unittest.TestCase):
    """The Python half of the same change: an unconfirmed lineup used to
    be a hard veto in is_actionable that no edge or tier could override,
    which made actionability track how early a slate's lineups happened to
    post. Lineup risk is still priced in elsewhere (confidence_score,
    NO_LINE_PENALTY/GETAWAY_DAY_PENALTY, the "Projected Lineup" badge) -
    just not as a veto."""

    def _under_band_row(self, **overrides):
        # Sits inside the validated UD UNDER 1.5-2.0 band - unconditionally
        # actionable once the lineup gate is out of the way.
        row = {"player_id": 1, "market_anchored": True, "edge": -1.7,
               "lineup_confirmed": False, "lineup_status": "projected"}
        row.update(overrides)
        return row

    def _cutoffs(self):
        return {"xwoba": float("inf"), "barrel": float("inf")}

    def test_unconfirmed_lineup_does_not_block_the_under_band(self):
        actionable, reason = report.is_actionable(self._under_band_row(), {}, 0.5, self._cutoffs())
        self.assertTrue(actionable)
        self.assertEqual(reason, "validated UNDER band")

    def test_unconfirmed_lineup_does_not_block_a_top25_over_with_signal(self):
        row = {"player_id": 1, "market_anchored": True,
               "edge": report.TOP25_OVER_EDGE_MIN, "platoon_advantage": "batter",
               "lineup_confirmed": False, "lineup_status": "projected"}
        actionable, reason = report.is_actionable(row, {}, 0.5, self._cutoffs())
        self.assertTrue(actionable)
        self.assertEqual(reason, "edge + platoon advantage")

    def test_confirmation_state_alone_changes_nothing(self):
        """The decisive assertion: identical rows differing ONLY in
        lineup_confirmed must produce the identical verdict and reason."""
        for overrides in ({"edge": -1.7}, {"edge": 0.0}):
            unconfirmed = report.is_actionable(
                self._under_band_row(lineup_confirmed=False, **overrides), {}, 0.5, self._cutoffs())
            confirmed = report.is_actionable(
                self._under_band_row(lineup_confirmed=True, lineup_status="confirmed", **overrides),
                {}, 0.5, self._cutoffs())
            self.assertEqual(unconfirmed, confirmed, f"row {overrides} still varies on lineup_confirmed")

    def test_the_old_veto_reason_string_is_gone_from_the_source(self):
        with open("report.py", encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("projected lineup - not confirmed", source,
                         "the removed veto's reason string must not survive anywhere in report.py")


class TestBuildCardStillReportsLineupStatus(unittest.TestCase):
    """Removing the treatment does not remove the information: build_card
    must keep emitting projectedLineup/lineupConfirmed, since the per-card
    "Projected Lineup" / "Confirmed Lineup" badges still render off them."""

    def _row(self, **overrides):
        row = {
            "player_id": 1, "name": "Test Player", "team": "Dodgers", "order": 3,
            "ud_pts": 10, "pp_pts": 8, "xwoba": 0.3, "barrel_pct": 10, "opp_era": 4.0,
            "weather": "N/A", "park_hr": 1.0, "platoon_edge": "N/A",
            "adjusted": False, "market_anchored": False, "no_line_penalty": False,
            "getaway_day_risk": False, "lineup_status": "confirmed", "lineup_confirmed": True,
            "edge": None, "ud_line": None, "game_time_pt": None, "game_date_utc": None,
            "actionable": True, "actionable_reason": "n/a", "top25_tier": None,
        }
        row.update(overrides)
        return row

    def test_confirmed_row_flags_confirmed(self):
        card = report.build_card(self._row())
        self.assertFalse(card["projectedLineup"])
        self.assertTrue(card["lineupConfirmed"])

    def test_projected_row_still_flags_projected_for_the_badge(self):
        card = report.build_card(self._row(lineup_status="projected", lineup_confirmed=False))
        self.assertTrue(card["projectedLineup"])
        self.assertFalse(card["lineupConfirmed"])
        # ...and being projected no longer costs it actionability.
        self.assertTrue(card["actionable"])


if __name__ == "__main__":
    unittest.main()
