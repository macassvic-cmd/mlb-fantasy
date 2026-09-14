"""
Soft "at-risk / potential DNS" watchlist - separate from the hard MLB/
soccer/NFL DNS detectors (stale_lines.py, soccer_dns.py, pro_league_
dns.py) and their Discord paths entirely. This is a WATCHLIST, not a
bet trigger: it surfaces players who are questionable/doubtful/GTD/
limited-practice/news-mention in public sources, cross-referenced
against whatever board file is dropped in, for a human to manually
confirm before acting. Never auto-flagged, never graded, never wired
into stale_lines.py/soccer_dns.py/pro_league_dns.py's state or Discord
paths - a completely independent script with its own incoming/processed/
failed folders, its own event log, and its own Discord digest.

CADENCE: run this every 5-15 minutes (its own scheduled task/local
runner loop, e.g. `python watchlist_dns.py --watch --interval 600`) -
NOT the hard detectors' 30-second loop. Soccer/NFL news moves over
hours, not minutes, and the NFL ESPN soft-tier fetch is expensive
(paginated refs + one detail GET per injury + one athlete GET per hit -
dozens of requests per team), which a 30s cadence would make excessive.

SOURCES (see this session's Phase 0 feasibility report before this was
built):
  MLB:    RotoWire MLB RSS (rss_news_classifier.py) - the SAME feed
          stale_lines.check_news() already polls for the hard detector,
          just capturing its "not_confirmed"/"news_mention"
          classifications instead of discarding them.
  NFL:    scrapers.espn_pro_leagues.fetch_team_injuries_soft_tiers() -
          confirmed live 2026-09-11 (real Questionable/practice-
          participation data pulled from a real team) - a NEW function,
          deliberately separate from fetch_team_injuries() so nothing
          here can touch the hard detector's confirmed-out path. PLUS
          RotoWire NFL RSS (watchlist_classifiers.py) as a second,
          independent source - confirmed live with real "Questionable"/
          "Listed as questionable" headlines matching the SAME real
          player (Jeremiyah Love) ESPN's source also flagged.
  Soccer: RotoWire Soccer RSS with its own vocabulary (late call/
          fifty-fifty/assessed/monitored/option/fitness-test - confirmed
          live against real headlines) PLUS Transfermarkt's "borderline
          return timing" tier - NOT a real probability tier (Transfermarkt
          has none - confirmed by inspecting a real club's table, every
          row is a confirmed diagnosis, never a likelihood grade) - see
          watchlist_classifiers.borderline_return_timing_hits()'s own
          docstring.

STATELESS BY DESIGN: no persistent early_signals-style tracking, no
dedup across runs. Each run recomputes "who's currently at risk" fresh
from the board file plus whatever the sources say RIGHT NOW - same
pattern soccer_dns.py's/pro_league_dns.py's own daily digest already
uses (a fresh compiled post every run, not "only post once per new
finding"). Staleness is computed directly from each hit's own timestamp
(the news item's pubDate, or the injury record's own date), not from
when this script first noticed it - there is no "first noticed" to
track.

OUTPUT: one compiled Discord digest per run (never per-player), marked
🧪 TESTING with the same visual language stale_lines.py's shadow-mode
Discord testing alerts use (this is a SEPARATE Discord code path from
that one, and from the hard flag alerts - see _post_digest() below;
none of them call each other). Everything is also logged to
data/watchlist/events.jsonl for a durable record independent of
Discord.
"""

import argparse
import ctypes
import json
import os
import shutil
import sys
import traceback
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

from board_loader import load_prop_records, _group_by_player, to_soccer_board
from rss_news_classifier import fetch_and_classify as fetch_mlb_news
from watchlist_classifiers import (
    fetch_nfl_watchlist_items, fetch_soccer_watchlist_items,
    borderline_return_timing_hits, tier_rank,
)
from scrapers.espn_pro_leagues import fetch_team_injuries_soft_tiers, match_espn_team_id
from scrapers.transfermarkt import get_team_injuries_cached

BASE_DIR = os.path.join("data", "watchlist")
INCOMING_DIR = os.path.join(BASE_DIR, "incoming")
PROCESSED_DIR = os.path.join(BASE_DIR, "processed")
FAILED_DIR = os.path.join(BASE_DIR, "failed")
EVENTS_LOG_PATH = os.path.join(BASE_DIR, "events.jsonl")
SPORTS = ["mlb", "soccer", "nfl"]
DEFAULT_WATCH_INTERVAL_SECONDS = 600  # 10 min - middle of the requested 5-15 min range

DISCORD_WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"
# Distinct from BOTH the hard flag's orange (0xE67E22) and the early-
# signal shadow tester's grey (0x95A5A6, stale_lines.EARLY_SIGNAL_TEST_
# COLOR) - yellow reads as "watch this, don't act on it" at a glance,
# a third distinct visual language for a third distinct trust level.
WATCHLIST_TEST_COLOR = 0xF1C40F
WATCHLIST_FOOTER = "UNVALIDATED SOURCE - manually verify. Not for action."


def _ensure_dirs():
    os.makedirs(INCOMING_DIR, exist_ok=True)
    for sport in SPORTS:
        os.makedirs(os.path.join(INCOMING_DIR, sport), exist_ok=True)
        os.makedirs(os.path.join(PROCESSED_DIR, sport), exist_ok=True)
        os.makedirs(os.path.join(FAILED_DIR, sport), exist_ok=True)


def _log_event(event):
    os.makedirs(BASE_DIR, exist_ok=True)
    with open(EVENTS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _staleness_minutes(iso_ts, now):
    """Minutes between iso_ts (a news item's pubDate, or an injury
    record's own date) and now - None if iso_ts is missing/unparseable,
    which the digest renders as "unknown" rather than a fake 0."""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return round((now - ts).total_seconds() / 60, 1)
    except Exception:
        return None


def _parse_date_only(iso_ts):
    if not iso_ts:
        return None
    try:
        return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).date()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-sport at-risk checks - each returns a flat list of hit dicts:
# {sport, name, team, live_line, tier, source, raw_text, staleness_minutes}
# plus an optional "practice_tier" (NFL/ESPN only).
# ---------------------------------------------------------------------------

def check_mlb_at_risk(records, now):
    by_name = _group_by_player(records, "mlb")
    if not by_name:
        return []
    hits = []
    try:
        news_items = fetch_mlb_news()
    except Exception as e:
        _log_event({"ts": now.isoformat(), "type": "source_error", "sport": "mlb", "source": "RotoWire MLB RSS", "error": str(e)})
        return []
    for item in news_items:
        if item["classification"] not in ("not_confirmed", "news_mention"):
            continue
        entry = by_name.get(item["normalized_name"])
        if entry is None:
            continue
        hits.append({
            "sport": "mlb", "name": entry["name"], "team": entry["team"],
            "live_line": entry["markets"], "tier": item["tier"] or item["classification"],
            "practice_tier": None,
            "source": "RotoWire MLB RSS", "raw_text": item["headline"],
            "staleness_minutes": _staleness_minutes(item.get("pub_date_utc"), now),
        })
    return hits


def check_nfl_at_risk(records, now):
    by_name = _group_by_player(records, "nfl")
    if not by_name:
        return []
    hits = []

    # Source 1: ESPN soft-tier fetch, once per team with a lined player.
    team_ids_checked = {}
    for entry in by_name.values():
        team_abbr = entry["team"]
        if team_abbr in team_ids_checked:
            continue
        try:
            team_ids_checked[team_abbr] = match_espn_team_id("nfl", team_abbr)
        except Exception as e:
            _log_event({"ts": now.isoformat(), "type": "source_error", "sport": "nfl", "source": "ESPN team match", "team": team_abbr, "error": str(e)})
            team_ids_checked[team_abbr] = None

    for team_abbr, team_id in team_ids_checked.items():
        if team_id is None:
            continue
        try:
            soft_records = fetch_team_injuries_soft_tiers("nfl", team_id)
        except Exception as e:
            _log_event({"ts": now.isoformat(), "type": "source_error", "sport": "nfl", "source": "ESPN injury report", "team": team_abbr, "error": str(e)})
            continue
        for r in soft_records:
            entry = by_name.get(r["normalized_name"])
            if entry is None:
                continue
            hits.append({
                "sport": "nfl", "name": entry["name"], "team": entry["team"],
                "live_line": entry["markets"], "tier": r["tier"], "practice_tier": r["practice_tier"],
                "source": "ESPN injury report", "raw_text": r["raw_text"],
                "staleness_minutes": _staleness_minutes(r.get("date_utc"), now),
            })

    # Source 2: RotoWire NFL RSS, fully independent of the ESPN pull above -
    # a player can show up from both, on purpose (cross-source agreement
    # is itself useful signal for the human doing the manual check).
    try:
        for item in fetch_nfl_watchlist_items():
            entry = by_name.get(item["normalized_name"])
            if entry is None:
                continue
            hits.append({
                "sport": "nfl", "name": entry["name"], "team": entry["team"],
                "live_line": entry["markets"], "tier": item["tier"], "practice_tier": None,
                "source": "RotoWire NFL RSS", "raw_text": item["headline"],
                "staleness_minutes": _staleness_minutes(item.get("pub_date_utc"), now),
            })
    except Exception as e:
        _log_event({"ts": now.isoformat(), "type": "source_error", "sport": "nfl", "source": "RotoWire NFL RSS", "error": str(e)})

    return hits


def _flatten_soccer_board(records):
    """to_soccer_board()'s nested {event_key: {teams: {team: {norm: {...}}}}}
    shape flattened to {normalized_name: {name, team, markets, fixture_date}}
    - reuses the SAME team-name resolution (_resolve_soccer_team_name) the
    hard soccer detector depends on, rather than re-deriving it, so a
    club's full legal name (what Transfermarkt's own lookup needs) is
    already correct here."""
    board = to_soccer_board(records)
    flat = {}
    for ev in board.values():
        fixture_date = _parse_date_only(ev.get("date"))
        for team_name, players in ev["teams"].items():
            for norm, p in players.items():
                flat[norm] = {"name": p["name"], "team": team_name, "markets": p["markets"], "fixture_date": fixture_date}
    return flat


def check_soccer_at_risk(records, now):
    by_name = _flatten_soccer_board(records)
    if not by_name:
        return []
    hits = []

    # Source 1: RotoWire Soccer RSS, own vocabulary.
    try:
        for item in fetch_soccer_watchlist_items():
            entry = by_name.get(item["normalized_name"])
            if entry is None:
                continue
            hits.append({
                "sport": "soccer", "name": entry["name"], "team": entry["team"],
                "live_line": entry["markets"], "tier": item["tier"], "practice_tier": None,
                "source": "RotoWire Soccer RSS", "raw_text": item["headline"],
                "staleness_minutes": _staleness_minutes(item.get("pub_date_utc"), now),
            })
    except Exception as e:
        _log_event({"ts": now.isoformat(), "type": "source_error", "sport": "soccer", "source": "RotoWire Soccer RSS", "error": str(e)})

    # Source 2: Transfermarkt borderline-return-timing, once per
    # (club, fixture_date) pair with at least one lined player - NOT once
    # per lined player, which would emit one duplicate hit per teammate
    # sharing that same fixture.
    today_str = now.strftime("%Y-%m-%d")
    seen_club_fixtures = set()
    for entry in by_name.values():
        team_name, fixture_date = entry["team"], entry["fixture_date"]
        if fixture_date is None or (team_name, fixture_date) in seen_club_fixtures:
            continue
        seen_club_fixtures.add((team_name, fixture_date))
        try:
            injuries = get_team_injuries_cached(team_name, today_str)
        except Exception as e:
            _log_event({"ts": now.isoformat(), "type": "source_error", "sport": "soccer", "source": "Transfermarkt", "team": team_name, "error": str(e)})
            continue
        for r in borderline_return_timing_hits(injuries, fixture_date):
            player_entry = by_name.get(r["normalized_name"])
            if player_entry is None or player_entry["team"] != team_name:
                continue  # on this club's injury list, but not THIS lined player/fixture
            hits.append({
                "sport": "soccer", "name": player_entry["name"], "team": team_name,
                "live_line": player_entry["markets"], "tier": r["tier"], "practice_tier": None,
                "source": "Transfermarkt (return-timing proxy)", "raw_text": r["raw_text"],
                "staleness_minutes": None,  # no publish timestamp - this is a standing list state, not a news event
            })

    return hits


CHECKERS = {"mlb": check_mlb_at_risk, "soccer": check_soccer_at_risk, "nfl": check_nfl_at_risk}


def _sort_key(hit):
    base = tier_rank(hit["tier"])
    if hit.get("practice_tier") == "dnp":
        base -= 0.5
    elif hit.get("practice_tier") == "limited":
        base -= 0.25
    return base


def _format_markets(markets):
    return ", ".join(f"{k} {v}" for k, v in sorted((markets or {}).items())) or "(none)"


def _format_staleness(minutes):
    if minutes is None:
        return "unknown"
    if minutes < 60:
        return f"{minutes:.0f} min ago"
    return f"{minutes / 60:.1f} hr ago"


def _digest_embed(hits, sport_label):
    hits_sorted = sorted(hits, key=_sort_key)
    lines = []
    for h in hits_sorted[:25]:  # Discord field-value length limit - see soccer_dns.py's own digest for the same cap reasoning
        practice_note = f" [{h['practice_tier']}]" if h.get("practice_tier") else ""
        lines.append(
            f"**{h['name']}** ({h['team']}) - `{h['tier']}`{practice_note} via {h['source']}, "
            f"{_format_staleness(h['staleness_minutes'])}\n"
            f"Line: {_format_markets(h['live_line'])}\n"
            f"> {h['raw_text'][:200]}"
        )
    overflow = len(hits_sorted) - 25
    body = "\n\n".join(lines) or "_No at-risk players found on this board._"
    if overflow > 0:
        body += f"\n\n_...and {overflow} more (see data/watchlist/events.jsonl for the full list)_"
    return {
        "title": f"\U0001F9EA TESTING — {sport_label} at-risk watchlist ({len(hits_sorted)} hit(s))",
        "description": "Soft watchlist - questionable/doubtful/GTD/limited-practice/news-mention. NOT a DNS flag.",
        "color": WATCHLIST_TEST_COLOR,
        "fields": [{"name": "At-risk players", "value": body[:5800], "inline": False}],
        "footer": {"text": WATCHLIST_FOOTER},
    }


def _post_digest(hits, sport_label):
    """Independent Discord path - does not call, share state with, or
    get called by stale_lines.py's/soccer_dns.py's/pro_league_dns.py's
    Discord functions. No-op if DISCORD_WEBHOOK_URL is unset or the post
    fails - matches every other Discord function in this project's
    never-break-detection-on-a-webhook-failure contract."""
    webhook_url = os.environ.get(DISCORD_WEBHOOK_ENV_VAR)
    if not webhook_url:
        return False
    try:
        resp = requests.post(f"{webhook_url}?wait=true", json={"embeds": [_digest_embed(hits, sport_label)]}, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"Watchlist Discord digest POST failed for {sport_label}: {e}")
        return False


def process_file(path, sport):
    records, skipped = load_prop_records(path, sport_hint=sport, skip_invalid=True)
    now = datetime.now(timezone.utc)
    hits = CHECKERS[sport](records, now)

    for h in hits:
        _log_event({"ts": now.isoformat(), "type": "watchlist_hit", **h})

    posted = _post_digest(hits, sport.upper())
    _log_event({
        "ts": now.isoformat(), "type": "digest_posted", "sport": sport,
        "hit_count": len(hits), "discord_posted": posted, "skipped_invalid_records": len(skipped),
    })
    return {"hit_count": len(hits), "skipped_invalid_records": len(skipped), "discord_posted": posted, "hits": hits}


def process_all_pending():
    _ensure_dirs()
    results = []
    for sport in SPORTS:
        in_dir = os.path.join(INCOMING_DIR, sport)
        for fname in sorted(os.listdir(in_dir)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(in_dir, fname)
            try:
                summary = process_file(path, sport)
                dest = os.path.join(PROCESSED_DIR, sport, fname)
                shutil.move(path, dest)
                print(f"[{sport}] watchlist processed {fname} -> {dest}\n  {summary['hit_count']} at-risk hit(s), discord_posted={summary['discord_posted']}")
                results.append({"file": fname, "sport": sport, "status": "ok", **summary})
            except Exception as e:
                dest = os.path.join(FAILED_DIR, sport, fname)
                shutil.move(path, dest)
                with open(dest + ".error.txt", "w", encoding="utf-8") as f:
                    f.write(f"{datetime.now(timezone.utc).isoformat()}\n{e}\n\n{traceback.format_exc()}")
                print(f"[{sport}] watchlist FAILED {fname}: {e} (moved to {dest})")
                results.append({"file": fname, "sport": sport, "status": "failed", "error": str(e)})
    if not results:
        print("Nothing pending in data/watchlist/incoming/{mlb,soccer,nfl}/.")
    return results


# Same pattern dabble_dns_local.py uses (see its own acquire_single_
# instance()) - added 2026-09-12 before registering this under a
# boot+logon scheduled task like the Dabble bridge's, which can double-
# fire both triggers on a single reboot; without this guard that would
# mean two --watch loops running at once, each posting its own duplicate
# Discord digest every cycle.
MUTEX_NAME = r"Global\MLBFantasy_WatchlistDNSLocal"
ERROR_ALREADY_EXISTS = 183


def _acquire_single_instance():
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise RuntimeError("CreateMutexW failed")
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        print("Another watchlist_dns.py --watch instance is already running.")
        sys.exit(0)
    return handle


def watch_forever(interval):
    import time
    mutex_handle = _acquire_single_instance()
    try:
        print(f"Watching {INCOMING_DIR}/ every {interval}s (5-15 min recommended) - Ctrl+C to stop.")
        while True:
            process_all_pending()
            time.sleep(interval)
    finally:
        ctypes.windll.kernel32.CloseHandle(mutex_handle)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=DEFAULT_WATCH_INTERVAL_SECONDS,
                         help=f"Polling interval in seconds for --watch mode (default {DEFAULT_WATCH_INTERVAL_SECONDS} - keep this at 300-900, not 30).")
    args = parser.parse_args()
    if args.interval < 300:
        parser.error("--interval below 300s (5 min) defeats the point of this poller being separate from the 30s hard-detector loop.")
    if args.watch:
        watch_forever(args.interval)
    else:
        process_all_pending()


if __name__ == "__main__":
    main()
