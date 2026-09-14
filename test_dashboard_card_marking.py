"""Regression tests for the 2026-09-14 "cards look pre-marked" bug.

Root cause: the diagonal-striped/dimmed ".pending-lineup" treatment (a
DATA-DRIVEN "lineup not confirmed yet" indicator) is a completely
different mechanism from the manual ".used"/"Clear all marks" click-to-
mark system, but visually the two are easy to confuse - a slate where
most players were incorrectly flagged lineup_confirmed=False (the Bug 1
stale-lineup issue) makes most cards look dimmed+faded, which reads as
"already marked" even though nobody clicked anything.

This repo has no JS execution harness (no Node/browser test runner), so
the ".used"/localStorage logic is verified the honest way available here:
structural assertions against the ACTUAL generated report.py source/HTML
- the exact bytes that ship - rather than a live DOM. build_card/row-level
behavior (the data feeding .pending-lineup) is tested directly in Python.

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
    for the REAL click-to-mark system (.used), not .pending-lineup."""

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


class TestPendingLineupIsIndependentOfUsedMarking(unittest.TestCase):
    """The actual mechanism behind the visual symptom: .pending-lineup is
    driven by today's real lineup-confirmation DATA (see pipeline.py's
    Bug 1 fix), never by usedIds/localStorage - the two must stay
    structurally independent so fixing one can never accidentally be
    confused with (or masked by) the other."""

    def setUp(self):
        with open("report.py", encoding="utf-8") as f:
            self.source = f.read()

    def test_pending_lineup_condition_does_not_reference_used_ids(self):
        m = re.search(r"const pending = \(([^)]*)\)", self.source)
        self.assertIsNotNone(m, "renderCard's pending-lineup condition must exist")
        condition = m.group(1)
        self.assertNotIn("usedIds", condition)
        self.assertIn("actionable", condition)
        self.assertIn("lineupConfirmed", condition)

    def test_pending_lineup_css_is_a_distinct_class_from_used(self):
        self.assertIn(".card.pending-lineup", self.source)
        self.assertIn(".card.used", self.source)
        # Different visual treatments - striped vs plain dim - confirming
        # they are not the same rule accidentally applying to both.
        pending_block = self.source[self.source.index(".card.pending-lineup"):][:300]
        used_block = self.source[self.source.index(".card.used {{"):][:120]
        self.assertIn("repeating-linear-gradient", pending_block)
        self.assertNotIn("repeating-linear-gradient", used_block)


class TestConfirmedLineupDataStopsPendingTreatment(unittest.TestCase):
    """Connects Bug 1's data fix to Bug 2's visual symptom directly: once
    a row is genuinely confirmed+actionable (what pipeline.py now
    produces once a lineup posts), build_card's output must give the
    frontend everything it needs to NOT apply .pending-lineup - the
    dashboard "just working" is a direct consequence of feeding it
    correct data, not a UI-side patch."""

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

    def test_confirmed_actionable_row_has_no_pending_indicators(self):
        card = report.build_card(self._row())
        self.assertFalse(card["projectedLineup"])
        self.assertTrue(card["lineupConfirmed"])
        self.assertTrue(card["actionable"])
        # This is exactly the (actionable=True) half of renderCard's
        # `!c.actionable && !c.lineupConfirmed` condition being false -
        # .pending-lineup cannot apply.

    def test_stale_projected_row_would_trigger_pending_state(self):
        """The Bug 1 symptom, reproduced at the data layer: a row that's
        still genuinely unconfirmed (or was, before the fix) DOES flip
        both flags the frontend gates on - proving .pending-lineup's
        broad appearance really did trace back to this data, not a CSS
        default."""
        card = report.build_card(self._row(lineup_status="projected", lineup_confirmed=False,
                                             actionable=False, actionable_reason="lineup not confirmed"))
        self.assertTrue(card["projectedLineup"])
        self.assertFalse(card["lineupConfirmed"])
        self.assertFalse(card["actionable"])


if __name__ == "__main__":
    unittest.main()
