"""Unit tests for dns_watch.py's mixed-sport file handling. Run with:
python -m unittest test_dns_watch -v"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from dns_watch import process_mixed_file


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class TestProcessMixedFile(unittest.TestCase):
    def test_routes_by_sport_and_skips_unsupported(self):
        data = {"props": [
            {"player_name": "A", "team": "NYY", "market": "HITS", "line": 1.5,
             "event_date": "2026-09-10", "sport": "mlb"},
            {"player_name": "B", "team": "Arsenal", "opponent": "Chelsea", "market": "SHOTS",
             "line": 1.5, "event_date": "2026-09-14", "sport": "soccer"},
            {"player_name": "C", "team": "LAL", "market": "POINTS", "line": 20.5,
             "event_date": "2026-09-10", "sport": "nba"},
        ]}
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            _write_json(path, data)
            with patch("stale_lines.run_poll", return_value="mlb-ok") as mock_mlb, \
                 patch("soccer_dns.run_scan", return_value="soccer-ok") as mock_soccer:
                summary = process_mixed_file(path)
            self.assertEqual(summary["mlb"], "mlb-ok")
            self.assertEqual(summary["soccer"], "soccer-ok")
            self.assertEqual(summary["skipped_unsupported_sports"], {"nba": 1})
            mock_mlb.assert_called_once()
            mock_soccer.assert_called_once()
        finally:
            os.remove(path)

    def test_only_mlb_present_soccer_not_called(self):
        data = {"props": [
            {"player_name": "A", "team": "NYY", "market": "HITS", "line": 1.5,
             "event_date": "2026-09-10", "sport": "mlb"},
        ]}
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            _write_json(path, data)
            with patch("stale_lines.run_poll", return_value="mlb-ok") as mock_mlb, \
                 patch("soccer_dns.run_scan", return_value="soccer-ok") as mock_soccer:
                summary = process_mixed_file(path)
            self.assertEqual(summary["mlb"], "mlb-ok")
            self.assertNotIn("soccer", summary)
            mock_soccer.assert_not_called()
        finally:
            os.remove(path)

    def test_raises_when_nothing_recognized(self):
        data = {"props": [
            {"player_name": "C", "team": "LAL", "market": "POINTS", "line": 20.5,
             "event_date": "2026-09-10", "sport": "nba"},
        ]}
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            _write_json(path, data)
            with self.assertRaises(ValueError) as ctx:
                process_mixed_file(path)
            self.assertIn("nba", str(ctx.exception))
        finally:
            os.remove(path)

    def test_record_missing_sport_in_root_file_raises(self):
        # No per-record sport AND no folder to fall back on for a default -
        # must fail loudly rather than silently dropping the record.
        data = {"props": [
            {"player_name": "A", "team": "NYY", "market": "HITS", "line": 1.5,
             "event_date": "2026-09-10"},
        ]}
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            _write_json(path, data)
            with self.assertRaises(ValueError) as ctx:
                process_mixed_file(path)
            self.assertIn("sport", str(ctx.exception))
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
