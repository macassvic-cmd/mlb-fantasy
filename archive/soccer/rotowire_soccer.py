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

import html
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


def _name_variant_candidates(player_name):
    """Alternate short forms to try when the exact full name doesn't
    match - found live 2026-09-14 auditing why RotoWire player-match
    coverage sat at only 42%: Dabble's soccer player_name field is the
    player's full LEGAL name ("Anthony Michael Gordon", "Cole Jermaine
    Palmer", "Eduardo Celmi Camavinga"), while RotoWire (like every
    other football source) indexes the common football name - almost
    always first-given-name + final-surname-word ("Anthony Gordon",
    "Cole Palmer", "Eduardo Camavinga" - confirmed live against all
    three). Each candidate here is still required to be an EXACT match
    against the real sitemap index elsewhere - this only generates
    plausible short forms, it never fuzzy-matches or guesses a
    "close enough" player."""
    words = player_name.split()
    if len(words) < 2:
        return []
    candidates = [f"{words[0]} {words[-1]}"]
    if len(words) >= 3:
        candidates.append(f"{words[0]} {words[1]}")  # Spanish paternal-surname convention
    return candidates


def resolve_player_page(player_name, index=None):
    """(url, player_id, ambiguous:bool, via_variant:bool) for player_
    name's RotoWire page, or None if no sitemap entry matches at all.
    Tries the exact full name first, then each of _name_variant_
    candidates's short forms - see that function's docstring for why a
    bare exact-match-only lookup badly undercounts real coverage.
    ambiguous=True means MULTIPLE distinct players share whichever name
    string actually matched - url/player_id are the first candidate
    only, surfaced for visibility, but callers should not treat a
    signal_found off an ambiguous match as reliably about THIS specific
    player.

    via_variant (2026-09-14 item 1, name-match audit) tells the caller
    whether this hit came from the EXACT full name (needs no further
    checking) or from a shortened variant - a real, if rare, risk: a
    DIFFERENT player who happens to share the same shortened form. See
    get_player_status, which requires an independent team confirmation
    before accepting a via_variant=True match."""
    index = index if index is not None else build_player_index()

    norm_exact = normalize_name(player_name)
    exact_candidates = index.get(norm_exact)
    if exact_candidates:
        return exact_candidates[0]["url"], exact_candidates[0]["player_id"], len(exact_candidates) > 1, False

    for candidate_name in _name_variant_candidates(player_name):
        norm = normalize_name(candidate_name)
        candidates = index.get(norm)
        if candidates:
            return candidates[0]["url"], candidates[0]["player_id"], len(candidates) > 1, True
    return None


_INJURY_CARD_RE = re.compile(
    r'p-card__injury">\s*<div class="tag">([^<]*)</div>(.*?)</div>\s*</div>\s*</div>', re.S)
_INJURY_DATA_RE = re.compile(r'p-card__injury-data">([^<]+)<b>([^<]*)</b>', re.S)

# Player-card header block ("Espanyol" / "La Liga", "Chicago Fire" / "MLS")
# - confirmed live 2026-09-14 across LaLiga and MLS player pages as a real,
# consistently-structured current-team field (distinct from the injury
# card's free text). Added for item 1's team-confirmation requirement: a
# name-VARIANT match (_name_variant_candidates) can coincidentally land on
# a different real player sharing the same shortened name - this is the
# independent fact that catches that case.
_TEAM_LEAGUE_RE = re.compile(r'font-weight:700">([^<]+)</div><div style="font-size: 14px;">([^<]+)</div>')


def _parse_player_team(page_html):
    """(team_name, league_name) off the page's team header block, or
    (None, None) if that exact block isn't present - never fabricates a
    team when the page layout doesn't match.

    html.unescape() is required, not cosmetic: confirmed live 2026-09-14
    that RotoWire's own markup renders this block as literal HTML
    entities ("Atl&eacute;tico Madrid"), which without unescaping would
    never string-equal ESPN's "Atlético Madrid" and would wrongly REJECT
    a genuinely correct team-confirmed match (found auditing item 1's
    name-match precision - Grimaldo, Hjulmand, and others all failed
    team confirmation for exactly this reason before this fix)."""
    m = _TEAM_LEAGUE_RE.search(page_html)
    return (html.unescape(m.group(1)).strip(), html.unescape(m.group(2)).strip()) if m else (None, None)


def _team_names_match(a, b):
    """Loose-but-real equality for two team display strings that may
    differ only by suffix/diacritics/language (RotoWire's short form vs
    ESPN's full display name) - normalize_name already strips diacritics
    and case; substring-tolerant both ways so a shortened or suffixed
    name on either side still counts as the same club. Never a fuzzy
    "close enough" guess across genuinely different clubs."""
    na, nb = normalize_name(a or ""), normalize_name(b or "")
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


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


def get_player_status(player_name, index=None, cache=None, use_disk_cache=True,
                       expected_team=None, match_log=None):
    """The full player-page-lookup result for one player - ALWAYS
    returns a dict, never None, so a caller can tell "checked, nothing
    wrong" apart from "never checked":

      attempted        - True (this function always tries the index)
      matched           - a sitemap entry exists for this name AND (if
                           via_variant) team confirmation passed
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
      via_variant       - True if the match came from a shortened name
                           variant rather than the exact full name
      team_confirmed    - True/False if via_variant (whether the page's
                           own team matched expected_team), None if not
                           applicable (exact match, no check needed)
      page_team         - the team RotoWire's own page shows, if fetched

    expected_team (2026-09-14 item 1, name-match audit): a real team
    display name to confirm against when the match came via a name
    variant - REQUIRED to accept a variant match at all. Missing or
    mismatched team on a variant match is rejected (treated the same as
    "not matched"), never silently accepted - see _team_names_match.
    Exact full-name matches need no team check (via_variant=False).

    match_log (optional, a list) - every variant-match ATTEMPT (accepted
    or rejected) is appended as a dict, for the coverage/precision audit
    (item 1) to inspect without re-fetching pages.

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
            "via_variant": False, "team_confirmed": None, "page_team": None,
        }
    else:
        url, player_id, ambiguous, via_variant = hit
        page_team = None
        try:
            html = _fetch_url(url, timeout=15)
            card = _parse_injury_card(html)
            page_team, _page_league = _parse_player_team(html)
        except Exception as e:
            print(f"rotowire_soccer: player page fetch failed for {player_name} (non-fatal): {e}")
            card = None

        team_confirmed = None
        reject_reason = None
        if via_variant:
            if not expected_team:
                team_confirmed, reject_reason = False, "no_expected_team_to_confirm"
            elif not page_team:
                team_confirmed, reject_reason = False, "page_team_unavailable"
            elif not _team_names_match(page_team, expected_team):
                team_confirmed, reject_reason = False, f"team_mismatch (page={page_team!r} expected={expected_team!r})"
            else:
                team_confirmed = True

        if match_log is not None:
            match_log.append({
                "player_name": player_name, "normalized_name": norm, "via_variant": via_variant,
                "page_url": url, "page_team": page_team, "expected_team": expected_team,
                "accepted": (not via_variant) or bool(team_confirmed), "reason": reject_reason,
            })

        if via_variant and not team_confirmed:
            print(f"rotowire_soccer: rejected partial match '{player_name}' -> {url} ({reject_reason})")
            result = {
                "attempted": True, "matched": False, "ambiguous": ambiguous, "page_url": None,
                "status_tag": None, "rotowire_status_normalized": None, "injury": None,
                "est_return": None, "signal_found": False, "source": "player_page", "checked_at": now_iso,
                "via_variant": via_variant, "team_confirmed": False, "page_team": page_team,
            }
        else:
            status_tag = card["tag"] if card else None
            result = {
                "attempted": True, "matched": True, "ambiguous": ambiguous, "page_url": url,
                "player_id": player_id, "status_tag": status_tag,
                "rotowire_status_normalized": ROTOWIRE_PAGE_TAG_TO_NORMALIZED.get((status_tag or "").lower()),
                "injury": card["injury"] if card else None, "est_return": card["est_return"] if card else None,
                "signal_found": card is not None, "source": "player_page", "checked_at": now_iso,
                "via_variant": via_variant, "team_confirmed": team_confirmed, "page_team": page_team,
            }

    # status_since (2026-09-16, Hinshelwood hard_out-floor item 2 conflict
    # check): the cache above only ever tracked checked_at (when we last
    # LOOKED), not when the status VALUE actually changed - with no way
    # to tell "hard_out since before his last start" (stale/conflicting)
    # apart from "hard_out since after it" (a real, current tag). Carry
    # the previous status_since forward when the tag is unchanged; reset
    # it to now only on an actual transition (including first-ever sight
    # of this player).
    prev_entry = cached_entry or disk_cache.get(norm)
    if prev_entry and prev_entry.get("status_tag") == result["status_tag"]:
        result["status_since"] = prev_entry.get("status_since", now_iso)
    else:
        result["status_since"] = now_iso

    if cache is not None:
        cache[norm] = result
    if use_disk_cache:
        disk_cache[norm] = result
        _save_status_cache(disk_cache)
    return result


def prefetch_player_statuses(player_specs, index=None, cache=None, use_disk_cache=True, max_workers=8,
                              match_log=None):
    """Warms `cache` (an in-process dict, mutated in place) for every
    (player_name, expected_team) pair in player_specs via a small thread
    pool - sequential player-page fetches for ~175 live Dabble players
    would otherwise take well over a minute every single scoring cycle
    (each name is a real HTTP request; confirmed live 2026-09-14 that
    this is what made soccer_dns.py --coverage exceed a 2-minute budget
    once player-page lookups were added). Pure performance optimization -
    get_player_status's per-name result/caching contract is unchanged,
    and this is safe to skip entirely (callers just get slower, not
    wrong, per-name fetches).

    expected_team travels alongside each name (rather than a bare name
    list) so get_player_status can still run its team-confirmation check
    (item 1) on a prefetched/cached result exactly as it would on a
    direct call - a plain list of names would silently skip that check
    for every prefetched player. match_log (optional, a list) collects
    every variant-match attempt across the whole batch - safe to share
    across threads (list.append is atomic under the GIL).

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
    for name, expected_team in player_specs:
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
        to_fetch.append((name, expected_team))

    def _fetch_one(spec):
        name, expected_team = spec
        return normalize_name(name), get_player_status(
            name, index=index, cache=None, use_disk_cache=False,
            expected_team=expected_team, match_log=match_log)

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
