"""Live UD line observation for forward CLV - one poll of the board.

Run from .github/workflows/pipeline.yml on EVERY cron fire (before, and
independent of, the freshness gate that skips the expensive pipeline
steps), so the day's snapshot accumulates a real open sample early and a
real last-before-lock sample late. See clv.py's module docstring for why
the 2026 render-time capture produced no usable data.

Usage:
  python clv_snapshot.py                          # observe today's live board
  python clv_snapshot.py --date 2027-04-01        # explicit date
  python clv_snapshot.py --lines-json lines.json  # inject lines instead of fetching
                                                  # ({"Player Name": 6.5, ...}; names are normalized)
  python clv_snapshot.py --date D --grade --results-path out.json
                                                  # grade D's snapshot into a results file
                                                  # (defaults to the real clv_results.json)

Always exits 0 - a failed fetch is logged in the summary, never allowed
to fail the workflow job that triggers it.
"""

import argparse
import json
import sys

import clv
from scrapers.market_lines import normalize_name
from scrapers.mlb_api import mlb_today_str


def _lines_from_json(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, dict) and "ud" in raw and isinstance(raw["ud"], dict):
        raw = raw["ud"]  # accept a market_lines cache file as-is
    return {normalize_name(k): float(v) for k, v in raw.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Observe the live UD board for forward CLV")
    parser.add_argument("--date", default=None, help="baseball day (default: today, Pacific-anchored)")
    parser.add_argument("--lines-json", default=None, help="read lines from this JSON file instead of fetching UD")
    parser.add_argument("--grade", action="store_true", help="grade the date's snapshot instead of observing")
    parser.add_argument("--results-path", default=None, help="with --grade: write results here instead of clv_results.json")
    args = parser.parse_args(argv)

    date_str = args.date or mlb_today_str()

    if args.grade:
        clv.grade_clv(date_str, results_path=args.results_path)
        return 0

    lines = _lines_from_json(args.lines_json) if args.lines_json else None
    summary = clv.capture_live_lines(date_str, lines=lines)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
