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

import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from watchlist_classifiers import _fetch_rss_items, classify_soccer_headline, SOCCER_RSS_URL
from rss_news_classifier import parse_title, parse_rss_pubdate
from scrapers.betr import normalize_name

# How stale an RSS item can be before we stop treating it as live signal -
# matches dnp_score.py's NEWS_LOOKBACK_HOURS reasoning (a 3-week-old story
# can't outweigh today's status - see item 6 of the soccer DNS request).
NEWS_LOOKBACK_HOURS = 72

# ---------------------------------------------------------------------------
# Player-page lookup (added 2026-09-14, coverage audit item 3) - the RSS
# feed above only ever surfaces a player who RotoWire happened to write a
# headline about TODAY (2/76 Dabble players on the day this was found).
# That's feed coverage, not player coverage: RotoWire's own soccer_players
# sitemap (34k+ entries, includes retired/lower-league players going back
# years) gives a real name -> player-page URL index, and every player page
# carries a live "p-card__injury" status card (tag/injury/est. return)
# independent of whether he made news today - confirmed live 2026-09-14
# that this is EXACTLY the signal the Dabble UI showed for Amar Dedic
# (GTD, hamstring, return 9/14/2026) that the feed-only path had been
# missing entirely.
#
# Two separate cached layers, deliberately different TTLs:
#   - the IDENTITY index (name -> page URL) barely changes; refreshed at
#     most once a day.
#   - each individual player's PAGE STATUS changes as often as RotoWire
#     updates it; refreshed every few hours per player, not per scoring
#     cycle (this can run many times a day - see soccer_adapter.py).
# ---------------------------------------------------------------------------
PLAYER_SITEMAP_URL = "https://www.rotowire.com/soccer_players.xml"
PLAYER_INDEX_PATH = os.path.join("data", "rotowire_soccer_player_index.json")
PLAYER_INDEX_TTL_HOURS = 24

PLAYER_STATUS_CACHE_PATH = os.path.join("data", "rotowire_soccer_player_status_cache.json")
PLAYER_STATUS_CACHE_TTL_HOURS = 4

_SITEMAP_URL_RE = re.compile(r"<loc>\s*(https://www\.rotowire\.com/soccer/player/([a-z0-9-]+)-(\d+))\s*</loc>", re.I)
_SLUG_ID_RE = re.compile(r"-(\d+)$")

_UA_HEADERS = {"User-Agent": "Mozilla/5.0"}

# RotoWire's player-page injury "tag" values (fantasy-style abbreviations,
# e.g. "GTD") mapped onto soccer_dns_score.ROTOWIRE_STATUS_PRIOR's EXISTING
# tiers - see that module's docstring for why no new tier/weight is added
# here (coverage audit explicitly deferred scoring-weight changes). An
# unrecognized tag still surfaces on the candidate (rotowire_status_raw)
# for visibility but maps to normalized=None, which soccer_dns_score's
# weight lookup already treats as a documented, safe no-op (0 points) -
# never fabricates a weight for a tag no one has reviewed.
ROTOWIRE_PAGE_TAG_TO_NORMALIZED = {
    "out": "hard_out",
    "susp": "hard_out",
    "ir": "hard_out",
    "gtd": "fifty-fifty",       # game-time decision - closest existing tier by intent
    "doubtful": "fifty-fifty",
    "questionable": "fifty-fifty",
    "dtd": "monitored",
}


def _fetch_url(url, timeout=20):
    req = urllib.request.Request(url, headers=_UA_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _slug_to_name(slug):
    """"amar-dedic-33528" -> "amar dedic" (the same shape normalize_name
    produces for a real player name, so it's directly comparable)."""
    base = _SLUG_ID_RE.sub("", slug)
    return base.replace("-", " ").strip()


def fetch_player_sitemap_entries():
    """[(normalized_name, url, player_id), ...] parsed straight out of
    RotoWire's own soccer_players.xml sitemap (34k+ <url> entries as of
    2026-09-14) - no HTML scraping needed, it's already a flat XML index.
    Regex-parsed rather than a full XML parser since the sitemap is huge
    and the structure is fixed/trivial (one <loc> per <url>); raises on a
    genuine fetch failure so build_player_index can tell "RotoWire is down"
    apart from "the index is just empty" ."""
    xml = _fetch_url(PLAYER_SITEMAP_URL, timeout=30)
    entries = []
    for m in _SITEMAP_URL_RE.finditer(xml):
        url, slug, player_id = m.group(1), m.group(2), m.group(3)
        entries.append((normalize_name(_slug_to_name(slug)), url, player_id))
    return entries


def build_player_index(force=False, path=PLAYER_INDEX_PATH):
    """{normalized_name: [{"url":, "player_id":}, ...]} - a list per name
    because a handful of names collide across different players (checked
    live: several distinct "mahamadou balde"/"balde" entries at different
    ids) - callers must treat >1 entry as ambiguous, never guess. Cached
    to disk and refreshed at most once per PLAYER_INDEX_TTL_HOURS; a
    fetch failure with an existing (even stale) cache on disk falls back
    to it rather than losing player-lookup coverage entirely."""
    if not force and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            if datetime.now(timezone.utc) - fetched_at < timedelta(hours=PLAYER_INDEX_TTL_HOURS):
                return cached["index"]
        except Exception:
            pass

    try:
        entries = fetch_player_sitemap_entries()
    except Exception as e:
        print(f"rotowire_soccer: player sitemap fetch failed (non-fatal): {e}")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)["index"]
            except Exception:
                pass
        return {}

    index = {}
    for norm_name, url, player_id in entries:
        index.setdefault(norm_name, []).append({"url": url, "player_id": player_id})

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(), "entry_count": len(entries),
                    "index": index}, f, indent=2, ensure_ascii=False)
    return index


def resolve_player_page(player_name, index=None):
    """(url, player_id, ambiguous:bool) for player_name's RotoWire page,
    or None if no sitemap entry matches at all. ambiguous=True means
    MULTIPLE distinct players share this normalized name - url/player_id
    are the first candidate only, surfaced for visibility, but callers
    should not treat a signal_found off an ambiguous match as reliably
    about THIS specific player."""
    index = index if index is not None else build_player_index()
    norm = normalize_name(player_name)
    candidates = index.get(norm)
    if not candidates:
        return None
    return candidates[0]["url"], candidates[0]["player_id"], len(candidates) > 1


_INJURY_CARD_RE = re.compile(
    r'p-card__injury">\s*<div class="tag">([^<]*)</div>(.*?)</div>\s*</div>\s*</div>', re.S)
_INJURY_DATA_RE = re.compile(r'p-card__injury-data">([^<]+)<b>([^<]*)</b>', re.S)


def _parse_injury_card(html):
    """{"tag":, "injury":, "est_return":} off a RotoWire player page's
    <div class="p-card__injury"> block, or None if the player has no
    current status card at all - confirmed live (Bukayo Saka, healthy)
    that this is a real "no signal" result, not a parsing miss: a
    healthy player's page simply omits the whole block."""
    m = _INJURY_CARD_RE.search(html)
    if not m:
        return None
    tag = (m.group(1) or "").strip()
    fields = {}
    for label, value in _INJURY_DATA_RE.findall(m.group(0)):
        label = label.strip().lower()
        if label.startswith("injury"):
            fields["injury"] = value.strip()
        elif label.startswith("est"):
            fields["est_return"] = value.strip()
    return {"tag": tag, "injury": fields.get("injury"), "est_return": fields.get("est_return")}


def fetch_player_page_status(url):
    """_parse_injury_card's result for a live fetch of `url` - re-raises
    on a network failure so get_player_status can distinguish "fetched,
    healthy" (None) from "couldn't check" (exception -> attempted=True,
    matched stays whatever the index said, signal stays unknown)."""
    html = _fetch_url(url, timeout=15)
    return _parse_injury_card(html)


def _load_status_cache(path=PLAYER_STATUS_CACHE_PATH):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_status_cache(cache, path=PLAYER_STATUS_CACHE_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def get_player_status(player_name, index=None, cache=None, use_disk_cache=True):
    """The full player-page-lookup result for one player - ALWAYS
    returns a dict, never None, so a caller can tell "checked, nothing
    wrong" apart from "never checked":

      attempted        - True (this function always tries the index)
      matched           - a sitemap entry exists for this name
      ambiguous         - multiple distinct players share this name
      page_url          - RotoWire's player page, if matched
      status_tag        - raw tag off the page ("GTD", "Out", ...), or
                           None if matched but currently healthy
      rotowire_status_normalized - ROTOWIRE_PAGE_TAG_TO_NORMALIZED's
                           mapping of status_tag, or None (unrecognized
                           tag, or no tag at all)
      injury / est_return - free text off the page, if present
      signal_found      - True only if matched AND a status card exists
      source            - "player_page" (always this value when matched)
      checked_at        - when this specific page fetch/cache-hit happened

    `cache` (optional, an in-process dict) lets one board-scoring run
    reuse a fetch across candidates without re-hitting disk; `use_disk_
    cache` persists across runs/cycles for PLAYER_STATUS_CACHE_TTL_HOURS
    so many polls a day (see soccer_adapter.py) don't re-fetch 76 pages
    every single time."""
    norm = normalize_name(player_name)
    if cache is not None and norm in cache:
        return cache[norm]

    disk_cache = _load_status_cache() if use_disk_cache else {}
    cached_entry = disk_cache.get(norm)
    if cached_entry:
        try:
            checked_at = datetime.fromisoformat(cached_entry["checked_at"])
            if datetime.now(timezone.utc) - checked_at < timedelta(hours=PLAYER_STATUS_CACHE_TTL_HOURS):
                if cache is not None:
                    cache[norm] = cached_entry
                return cached_entry
        except Exception:
            pass

    hit = resolve_player_page(player_name, index=index)
    now_iso = datetime.now(timezone.utc).isoformat()
    if hit is None:
        result = {
            "attempted": True, "matched": False, "ambiguous": False, "page_url": None,
            "status_tag": None, "rotowire_status_normalized": None, "injury": None,
            "est_return": None, "signal_found": False, "source": "player_page", "checked_at": now_iso,
        }
    else:
        url, player_id, ambiguous = hit
        try:
            card = fetch_player_page_status(url)
        except Exception as e:
            print(f"rotowire_soccer: player page fetch failed for {player_name} (non-fatal): {e}")
            card = None
        status_tag = card["tag"] if card else None
        result = {
            "attempted": True, "matched": True, "ambiguous": ambiguous, "page_url": url,
            "player_id": player_id, "status_tag": status_tag,
            "rotowire_status_normalized": ROTOWIRE_PAGE_TAG_TO_NORMALIZED.get((status_tag or "").lower()),
            "injury": card["injury"] if card else None, "est_return": card["est_return"] if card else None,
            "signal_found": card is not None, "source": "player_page", "checked_at": now_iso,
        }

    if cache is not None:
        cache[norm] = result
    if use_disk_cache:
        disk_cache[norm] = result
        _save_status_cache(disk_cache)
    return result


def prefetch_player_statuses(player_names, index=None, cache=None, use_disk_cache=True, max_workers=8):
    """Warms `cache` (an in-process dict, mutated in place) for every
    name in player_names via a small thread pool - sequential player-page
    fetches for ~76 live Dabble players would otherwise take well over a
    minute every single scoring cycle (each name is a real HTTP request;
    confirmed live 2026-09-14 that this is what made soccer_dns.py
    --coverage exceed a 2-minute budget once player-page lookups were
    added). Pure performance optimization - get_player_status's per-name
    result/caching contract is unchanged, and this is safe to skip
    entirely (callers just get slower, not wrong, per-name fetches).

    Loads/saves the on-disk cache ONCE for the whole batch (rather than
    once per name, which would race across threads) - names already
    fresh in `cache` or the disk cache within PLAYER_STATUS_CACHE_TTL_
    HOURS are skipped entirely, so a second call this run (or a run
    within the TTL window) does no network I/O at all."""
    import concurrent.futures

    index = index if index is not None else build_player_index()
    cache = cache if cache is not None else {}
    disk_cache = _load_status_cache() if use_disk_cache else {}
    now = datetime.now(timezone.utc)

    to_fetch = []
    seen_norms = set()
    for name in player_names:
        norm = normalize_name(name)
        if norm in seen_norms:
            continue
        seen_norms.add(norm)
        if norm in cache:
            continue
        cached_entry = disk_cache.get(norm)
        if cached_entry:
            try:
                checked_at = datetime.fromisoformat(cached_entry["checked_at"])
                if now - checked_at < timedelta(hours=PLAYER_STATUS_CACHE_TTL_HOURS):
                    cache[norm] = cached_entry
                    continue
            except Exception:
                pass
        to_fetch.append(name)

    def _fetch_one(name):
        return normalize_name(name), get_player_status(name, index=index, cache=None, use_disk_cache=False)

    if to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            for norm, result in ex.map(_fetch_one, to_fetch):
                cache[norm] = result
                disk_cache[norm] = result
        if use_disk_cache:
            _save_status_cache(disk_cache)

    return cache


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
