"""Unit tests for clv.py's v2 capture/grade path (2026-09-29 rewrite).

The property under test is the one the 2026 season lacked: two live
observations of a different line, taken at different times, must be stored
as two timestamped samples, and grading must produce a NON-push CLV from
them. Also covers: no duplicate samples when the line hasn't moved,
post-lock observations ignored, close = last pre-lock sample, single-sample
plays reported as pushes but counted separately, and the legacy 2026 file
format still grading.

Run with: python -m unittest test_clv -v
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import clv

DATE = "2027-04-01"
T0 = datetime(2027, 4, 1, 14, 0, 0, tzinfo=timezone.utc)   # 7am PT
LOCK = T0 + timedelta(hours=9)                              # 4pm PT first pitch


def _row(name, edge, actionable=True, pid=1, lock=LOCK):
    return {"name": name, "player_id": pid, "team": "Test Team", "market_anchored": True,
            "ud_line": 6.5, "edge": edge, "actionable": actionable,
            "game_date_utc": lock.strftime("%Y-%m-%dT%H:%M:%SZ")}


class ClvV2TestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (clv.SNAPSHOTS_DIR, clv.CLV_RESULTS_PATH, clv.DATA_DIR)
        clv.SNAPSHOTS_DIR = os.path.join(self.tmp, "clv_snapshots")
        clv.CLV_RESULTS_PATH = os.path.join(self.tmp, "results", "clv_results.json")
        clv.DATA_DIR = os.path.join(self.tmp, "data")
        os.makedirs(clv.DATA_DIR)

    def tearDown(self):
        clv.SNAPSHOTS_DIR, clv.CLV_RESULTS_PATH, clv.DATA_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_day_data(self, players):
        with open(os.path.join(clv.DATA_DIR, f"{DATE}.json"), "w", encoding="utf-8") as f:
            json.dump(players, f)

    def _snap(self):
        return clv.load_snapshot(DATE)

    # -- capture ----------------------------------------------------------

    def test_two_different_lines_become_two_timestamped_samples(self):
        s1 = clv.capture_live_lines(DATE, lines={"seiya suzuki": 6.5}, now=T0)
        s2 = clv.capture_live_lines(DATE, lines={"seiya suzuki": 7.5}, now=T0 + timedelta(hours=6))
        self.assertEqual(s1["n_new_samples"], 1)
        self.assertEqual(s2["n_new_samples"], 1)
        self.assertEqual(s2["n_players_multi_sample"], 1)
        samples = self._snap()["players"]["seiya suzuki"]["samples"]
        self.assertEqual([s["line"] for s in samples], [6.5, 7.5])
        self.assertEqual(samples[0]["at"], T0.isoformat())
        self.assertEqual(samples[1]["at"], (T0 + timedelta(hours=6)).isoformat())

    def test_unchanged_line_does_not_duplicate_but_updates_last_seen(self):
        clv.capture_live_lines(DATE, lines={"seiya suzuki": 6.5}, now=T0)
        s2 = clv.capture_live_lines(DATE, lines={"seiya suzuki": 6.5}, now=T0 + timedelta(hours=1))
        self.assertEqual(s2["n_new_samples"], 0)
        entry = self._snap()["players"]["seiya suzuki"]
        self.assertEqual(len(entry["samples"]), 1)
        self.assertEqual(entry["last_seen_at"], (T0 + timedelta(hours=1)).isoformat())

    def test_observation_after_lock_is_ignored(self):
        self._write_day_data([{"name": "Seiya Suzuki", "player_id": 1, "team_name": "Chicago Cubs",
                               "game_date_utc": LOCK.strftime("%Y-%m-%dT%H:%M:%SZ")}])
        clv.capture_live_lines(DATE, lines={"seiya suzuki": 6.5}, now=T0)
        clv.capture_live_lines(DATE, lines={"seiya suzuki": 9.5}, now=LOCK + timedelta(minutes=5))
        entry = self._snap()["players"]["seiya suzuki"]
        self.assertEqual([s["line"] for s in entry["samples"]], [6.5])
        self.assertEqual(entry["player_id"], 1)
        self.assertEqual(entry["lock_at"], LOCK.strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_fetch_failure_is_reported_not_raised(self):
        def boom():
            raise ConnectionError("UD down")
        s = clv.capture_live_lines(DATE, fetch=boom, now=T0)
        self.assertFalse(s["fetched"])
        self.assertIn("UD down", s["error"])
        self.assertIsNone(self._snap())

    def test_pre_pipeline_capture_gets_metadata_backfilled_later(self):
        clv.capture_live_lines(DATE, lines={"seiya suzuki": 6.5}, now=T0)  # no data file yet
        self.assertIsNone(self._snap()["players"]["seiya suzuki"]["lock_at"])
        self._write_day_data([{"name": "Seiya Suzuki", "player_id": 1, "team_name": "Chicago Cubs",
                               "game_date_utc": LOCK.strftime("%Y-%m-%dT%H:%M:%SZ")}])
        clv.capture_live_lines(DATE, lines={"seiya suzuki": 6.5}, now=T0 + timedelta(hours=2))
        entry = self._snap()["players"]["seiya suzuki"]
        self.assertEqual(entry["lock_at"], LOCK.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(entry["name"], "Seiya Suzuki")

    # -- annotations ------------------------------------------------------

    def test_annotation_carries_call_but_no_line(self):
        clv.record_line_snapshot(DATE, [_row("Seiya Suzuki", edge=1.8)], now=T0)
        ann = self._snap()["annotations"]["seiya suzuki"]
        self.assertEqual(ann["call"], "over")
        self.assertTrue(ann["actionable"])
        self.assertNotIn("open_line", ann)
        self.assertNotIn("line", ann)

    def test_annotation_not_overwritten_after_lock(self):
        clv.record_line_snapshot(DATE, [_row("Seiya Suzuki", edge=1.8)], now=T0)
        clv.record_line_snapshot(DATE, [_row("Seiya Suzuki", edge=-1.8)], now=LOCK + timedelta(hours=1))
        self.assertEqual(self._snap()["annotations"]["seiya suzuki"]["call"], "over")

    # -- grading ----------------------------------------------------------

    def test_grade_produces_non_push_clv_from_two_samples(self):
        clv.capture_live_lines(DATE, lines={"seiya suzuki": 6.5, "steven kwan": 8.5}, now=T0)
        clv.record_line_snapshot(DATE, [_row("Seiya Suzuki", edge=1.8, pid=1),
                                        _row("Steven Kwan", edge=-1.7, pid=2)], now=T0 + timedelta(hours=1))
        clv.capture_live_lines(DATE, lines={"seiya suzuki": 7.5, "steven kwan": 7.5}, now=T0 + timedelta(hours=7))
        data = clv.grade_clv(DATE)
        by_name = {p["name"]: p for p in data["dates"][DATE]["plays"]}
        self.assertEqual(by_name["Seiya Suzuki"]["clv_points"], 1.0)   # OVER, line rose  -> +1.0
        self.assertEqual(by_name["Steven Kwan"]["clv_points"], 1.0)    # UNDER, line fell -> +1.0
        self.assertEqual(by_name["Seiya Suzuki"]["n_samples"], 2)
        self.assertEqual(by_name["Seiya Suzuki"]["open_at"], T0.isoformat())
        self.assertEqual(by_name["Seiya Suzuki"]["close_at"], (T0 + timedelta(hours=7)).isoformat())
        rec = data["record"]
        self.assertEqual((rec["positive"], rec["negative"], rec["push"], rec["n"]), (2, 0, 0, 2))
        self.assertEqual(rec["plays_multi_sample"], 2)
        self.assertEqual(rec["avg_clv_points"], 1.0)

    def test_close_is_last_pre_lock_sample_even_if_lock_unknown_at_capture(self):
        clv.capture_live_lines(DATE, lines={"seiya suzuki": 6.5}, now=T0)
        clv.capture_live_lines(DATE, lines={"seiya suzuki": 7.0}, now=T0 + timedelta(hours=3))
        clv.record_line_snapshot(DATE, [_row("Seiya Suzuki", edge=1.8)], now=T0 + timedelta(hours=4))  # sets lock_at
        # a stray reading after lock - capture drops it now that lock_at is known
        clv.capture_live_lines(DATE, lines={"seiya suzuki": 9.0}, now=LOCK + timedelta(minutes=1))
        data = clv.grade_clv(DATE)
        play = data["dates"][DATE]["plays"][0]
        self.assertEqual((play["open_line"], play["close_line"], play["clv_points"]), (6.5, 7.0, 0.5))

    def test_single_sample_is_push_but_counted_separately(self):
        clv.capture_live_lines(DATE, lines={"seiya suzuki": 6.5}, now=T0)
        clv.record_line_snapshot(DATE, [_row("Seiya Suzuki", edge=1.8)], now=T0)
        rec = clv.grade_clv(DATE)["record"]
        self.assertEqual((rec["push"], rec["plays_single_sample"], rec["plays_multi_sample"]), (1, 1, 0))

    def test_annotated_play_with_no_live_samples_is_skipped_and_counted(self):
        clv.record_line_snapshot(DATE, [_row("Seiya Suzuki", edge=1.8)], now=T0)
        data = clv.grade_clv(DATE)
        self.assertEqual(data["dates"][DATE]["plays"], [])
        self.assertEqual(data["record"]["skipped_no_samples"], 1)

    def test_non_actionable_and_neutral_rows_not_graded(self):
        clv.capture_live_lines(DATE, lines={"a b": 6.5, "c d": 6.5}, now=T0)
        clv.record_line_snapshot(DATE, [_row("A B", edge=1.8, actionable=False, pid=1),
                                        _row("C D", edge=0.2, pid=2)], now=T0)
        clv.capture_live_lines(DATE, lines={"a b": 7.5, "c d": 7.5}, now=T0 + timedelta(hours=2))
        self.assertEqual(clv.grade_clv(DATE)["record"]["n"], 0)

    def test_legacy_2026_snapshot_still_grades(self):
        os.makedirs(clv.SNAPSHOTS_DIR)
        legacy = {"547180": {"name": "Bryce Harper", "team": "PHI", "market": "ud", "open_line": 6.5,
                             "close_line": 6.5, "call": "under", "actionable_at_close": True,
                             "open_at": "2026-09-27T10:12:00+00:00", "close_at": "2026-09-28T04:17:00+00:00"}}
        with open(os.path.join(clv.SNAPSHOTS_DIR, "2026-09-27.json"), "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        rec = clv.grade_clv("2026-09-27")["record"]
        self.assertEqual((rec["push"], rec["n"], rec["plays_single_sample"]), (1, 1, 1))

    def test_capture_refuses_to_touch_legacy_file(self):
        os.makedirs(clv.SNAPSHOTS_DIR)
        with open(os.path.join(clv.SNAPSHOTS_DIR, f"{DATE}.json"), "w", encoding="utf-8") as f:
            json.dump({"1": {"open_line": 6.5}}, f)
        s = clv.capture_live_lines(DATE, lines={"x y": 6.5}, now=T0)
        self.assertEqual(s["error"], "legacy snapshot present")
        self.assertTrue(clv.is_legacy_snapshot(clv.load_snapshot(DATE)))


if __name__ == "__main__":
    unittest.main()
