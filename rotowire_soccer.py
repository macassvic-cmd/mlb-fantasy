"""
RotoWire soccer ingestion - the legitimate access path available to this
codebase is RotoWire's public RSS feed (no login/paywall bypass), already
wired up as watchlist_classifiers.fetch_soccer_watchlist_items() /
classify_soccer_headline() (SOCCER_SOFT_TIER_PATTERNS: fifty-fifty,
late-call, fitness-test, monitored, assessed, option). This module doesn't
re-scrape RotoWire - it wraps that existing capability into the per-player
status shape soccer_adapter.py's consensus layer needs.

Honest limitation: RotoWire's full structured injury-report TABLE (a
per-player OUT/GTD/Doubtful grid, distinct from RSS headlines) sits behind
a page this repo has no authenticated access to - built here is real
(RSS headline classification, timestamped), not a paywall bypass of that
table. rotowire_predicted_start is always None for the same reason - RSS
headlines are a status/injury feed, not a predicted-lineup feed.
"""

import os
from datetime import datetime, timezone

from watchlist_classifiers import _fetch_rss_items, classify_soccer_headline, SOCCER_RSS_URL
from rss_news_classifier import parse_title, parse_rss_pubdate
from scrapers.betr import normalize_name

# How stale an RSS item can be before we stop treating it as live signal -
# matches dnp_score.py's NEWS_LOOKBACK_HOURS reasoning (a 3-week-old story
# can't outweigh today's status - see item 6 of the soccer DNS request).
NEWS_LOOKBACK_HOURS = 72


def fetch_rotowire_soccer_status(lookback_hours=NEWS_LOOKBACK_HOURS):
    """{normalized_name: {rotowire_status_raw, rotowire_status_normalized,
    rotowire_injury, rotowire_news_at, rotowire_headline,
    rotowire_predicted_start, fetched_at}} - the most recent RSS hit per
    player within lookback_hours, for EVERY classification including
    "hard_out" and "reversal".

    Deliberately does NOT use watchlist_classifiers.fetch_soccer_
    watchlist_items() - that function intentionally drops hard_out/
    reversal items because it feeds a different consumer (a soft-signal
    watchlist that defers to the hard Transfermarkt-based detector for
    confirmed-out cases). For DNS scoring, "hard_out" is exactly the
    highest-value signal (found 2026-09-14 auditing why real injury
    headlines like "Out six to eight weeks" weren't reaching soccer_
    adapter.py at all) - so this classifies the raw feed itself instead.

    Best-effort: an RSS fetch failure returns {} (no RotoWire signal),
    never raises - this must not block scoring for the rest of the board."""
    fetched_at = datetime.now(timezone.utc)
    try:
        raw_items = _fetch_rss_items(SOCCER_RSS_URL)
    except Exception as e:
        print(f"rotowire_soccer: RSS fetch failed (non-fatal): {e}")
        return {}

    by_player = {}
    for raw_item in raw_items:
        name, headline = parse_title(raw_item["title"])
        if name is None:
            continue
        classification, tier = classify_soccer_headline(headline)
        if classification is None:
            continue  # not injury/status-adjacent at all - not a watchlist bug, just not relevant

        pub_dt = parse_rss_pubdate(raw_item.get("pub_date"))
        if pub_dt is not None:
            age_hours = (fetched_at - pub_dt).total_seconds() / 3600
            if age_hours > lookback_hours:
                continue

        norm = normalize_name(name)
        existing = by_player.get(norm)
        if existing and existing.get("_pub_dt") and pub_dt and pub_dt <= existing["_pub_dt"]:
            continue  # most recent wins if multiple RSS hits for the same player

        by_player[norm] = {
            "rotowire_status_raw": tier or classification,
            "rotowire_status_normalized": classification,  # "soft"|"news_mention"|"reversal"|"hard_out"
            "rotowire_injury": headline,
            "rotowire_news_at": pub_dt.isoformat() if pub_dt else None,
            "rotowire_headline": headline,
            "rotowire_predicted_start": None,  # not available via RSS - see module docstring
            "fetched_at": fetched_at.isoformat(),
            "_pub_dt": pub_dt,
        }

    for v in by_player.values():
        v.pop("_pub_dt", None)
    return by_player
