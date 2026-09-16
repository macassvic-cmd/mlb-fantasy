"""
Soccer DNS adapter - the Dabble board IS the candidate universe, exactly
like MLB's dabble_adapter.py (see 2026-09-14 audit that found Amar Dedic/
Mama Balde missing because NO soccer board had ever actually been
produced/ingested at all - the external producer hardcoded sport="mlb"
unconditionally; fixed separately in platform-tools/dabble_board_producer.py).

Architecture (do not violate this - it's the whole point of the fix):
  Dabble soccer board -> ALL live soccer players -> normalize -> enrich
  from every available soccer source -> score P(NOT START)

External sources (Transfermarkt injuries, RotoWire RSS, X, soccer start
history, predicted-XI consensus) enrich a candidate. They never decide who
exists. A player who fails every enrichment source still becomes a
candidate, marked LOW DATA / ENRICHMENT MISSING (see enrich_and_score_
player's watchdog handling) rather than silently dropped - see item 10.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import rotowire_soccer
import soccer_dns_score
import soccer_start_history
import soccer_x_monitor
from board_loader import _LEAGUE_VALUE_TO_CANONICAL
from rotowire_soccer import fetch_rotowire_soccer_status
from scrapers.betr import normalize_name
from scrapers.espn_soccer import (match_espn_team_id, resolve_team_by_code, find_espn_event,
                                   get_match_squad_names, team_display_name, LEAGUE_SLUGS)
from scrapers.transfermarkt import get_team_injuries_cached

DEFAULT_BOARD_PATH = os.path.join("data", "dns_watch", "processed", "soccer", "dabble_soccer_board.json")
LIVE_SNAPSHOTS_DIR = os.path.join("data", "soccer_dnp_live_snapshots")
THRESHOLDS_TO_TRACK = [60, 70, 80, 90]


def _prop_id(normalized_name, team, market, line):
    raw = f"{normalized_name}:{team}:{market}:{line}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Step 1: raw Dabble board -> deduped per-prop records (no league/sport
# filtering beyond "is this soccer at all" - see module docstring)
# ---------------------------------------------------------------------------

def load_dabble_soccer_props(source=DEFAULT_BOARD_PATH, scraped_at=None):
    if isinstance(source, (str, os.PathLike)):
        if not os.path.exists(source):
            print(f"soccer_adapter: no board file at {source}.")
            return []
        with open(source, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = source

    if isinstance(data, dict):
        props_raw = data.get("props", [])
        file_scraped_at = data.get("generated_at")
        file_sport = data.get("sport")
    elif isinstance(data, list):
        props_raw = data
        file_scraped_at = None
        file_sport = None
    else:
        raise ValueError("Dabble soccer board must be a dict with 'props' or a bare list.")

    scraped_at = scraped_at or file_scraped_at or datetime.now(timezone.utc).isoformat()

    seen_ids = set()
    props = []
    skipped = 0
    for raw in props_raw:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        sport = str(raw.get("sport") or file_sport or "").strip().lower()
        if sport not in ("soccer", "football"):
            continue  # not our sport - not malformed, just not relevant here

        player_name = raw.get("player_name")
        team = raw.get("team")
        market = raw.get("market")
        line = raw.get("line")
        event_date = raw.get("event_date")
        if not player_name or not team or not market or line is None or not event_date:
            skipped += 1
            continue
        try:
            line = float(line)
        except (TypeError, ValueError):
            skipped += 1
            continue

        norm = normalize_name(str(player_name))
        pid = _prop_id(norm, str(team), str(market), line)
        if pid in seen_ids:
            continue
        seen_ids.add(pid)

        props.append({
            "player_name": str(player_name).strip(),
            "normalized_name": norm,
            "dabble_player_id": raw.get("player_id"),
            "team": str(team).strip(),
            "market": str(market).strip(),
            "line": line,
            "prop_id": pid,
            "scraped_at": scraped_at,
            "matchup": raw.get("matchup"),
            "event_date": str(event_date).strip(),
            "league_raw": raw.get("league"),
            "fixture_id": raw.get("fixture_id"),
            "position": (str(raw["position"]).strip() if raw.get("position") else None),
            "dabble_status_raw": raw.get("dabble_status_raw"),
            "dabble_status_normalized": raw.get("dabble_status_normalized"),
        })

    if skipped:
        print(f"soccer_adapter: skipped {skipped} malformed/unusable prop record(s) out of {len(props_raw)}.")
    return props


def group_props_by_player(props):
    """One record per (normalized_name, team) regardless of market count -
    same dedup point as MLB's dabble_adapter.group_props_by_player. No
    eligibility filtering by league/position here - see module docstring."""
    players = {}
    for p in props:
        key = (p["normalized_name"], p["team"])
        entry = players.setdefault(key, {
            "player_name": p["player_name"],
            "normalized_name": p["normalized_name"],
            "dabble_player_id": p["dabble_player_id"],
            "team": p["team"],
            "position": p["position"],
            "event_date": p["event_date"],
            "matchup": p["matchup"],
            "league_raw": p["league_raw"],
            "fixture_id": p["fixture_id"],
            "dabble_status_raw": p["dabble_status_raw"],
            "dabble_status_normalized": p["dabble_status_normalized"],
            "props": [],
        })
        if p["position"] and not entry["position"]:
            entry["position"] = p["position"]
        entry["props"].append({"market": p["market"], "line": p["line"],
                                "prop_id": p["prop_id"], "scraped_at": p["scraped_at"]})
    return players


# ---------------------------------------------------------------------------
# Step 2: enrichment context - built once per board, shared across players
# ---------------------------------------------------------------------------

def _league_code(league_raw):
    if not league_raw:
        return None
    return _LEAGUE_VALUE_TO_CANONICAL.get(str(league_raw).strip().lower())


def _expected_team_display_name(league_code, team_code):
    """A real, ESPN-verified club display name for a Dabble board's short
    team code, or None if it can't be resolved to a real name at all
    (2026-09-14 item 1, name-match audit) - used as the ground truth a
    RotoWire name-VARIANT match must be confirmed against
    (rotowire_soccer.get_player_status's expected_team). Deliberately
    returns None rather than the bare code itself when unresolved
    (team_display_name's own no-match fallback) - an unresolved code
    must never be treated as if it "matched" some short RotoWire
    abbreviation by coincidence."""
    if not league_code:
        return None
    exact = resolve_team_by_code(league_code, team_code)
    if exact:
        return exact[1]
    name = team_display_name(league_code, team_code)
    return name if name != team_code else None


def _resolve_espn_team_id(player, context, league_code):
    """ESPN's numeric team id for player['team'] (a Dabble short code) -
    the same id space soccer_start_history's recorded matches use for
    team_id, so it doubles as the team-confirmation anchor for a
    history name-variant match (item 1). Cached in context, shared with
    _enrich_espn_official_status's own (independent) need for the same
    id - this is a second call into the same cache, not a second real
    lookup.

    Competition-agnostic fallback (2026-09-17, item 2) - found live that
    Jack Hinshelwood's League Cup fixture had league_code=None (that
    specific raw competition string had no mapping at all), which used
    to mean team identity was NEVER resolved for him even though
    Brighton is a perfectly normal, always-resolvable EPL club. A club's
    numeric ESPN team id is the SAME across every competition it plays
    in (confirmed live: Brighton is id 331 under both eng.1 and
    eng.league_cup), so when this fixture's own competition is unmapped
    or the direct lookup fails, this tries every OTHER mapped
    competition before giving up - the club only needs to be findable
    under ONE of them, not specifically the one today's fixture is filed
    under. board_loader.LEAGUE_VALUE_ALIASES now also maps every cup/
    European competition seen on the board (so most fixtures resolve
    directly here anyway), and this fallback additionally covers any
    STILL-unmapped competition for a club that also plays domestically."""
    team_cache_key = (league_code, player["team"])
    if team_cache_key in context["espn_team_cache"]:
        return context["espn_team_cache"][team_cache_key]

    resolved = None
    if league_code and league_code in LEAGUE_SLUGS:
        try:
            resolved = match_espn_team_id(league_code, player["team"])
        except Exception:
            resolved = None

    if resolved is None:
        for other_code in LEAGUE_SLUGS:
            if other_code == league_code:
                continue
            other_key = (other_code, player["team"])
            if other_key in context["espn_team_cache"]:
                candidate = context["espn_team_cache"][other_key]
            else:
                try:
                    candidate = match_espn_team_id(other_code, player["team"])
                except Exception:
                    candidate = None
                context["espn_team_cache"][other_key] = candidate
            if candidate:
                resolved = candidate
                break

    context["espn_team_cache"][team_cache_key] = resolved
    return resolved


def _build_enrichment_context(players):
    context = {
        "rotowire": {},
        "rotowire_player_index": {},
        "rotowire_status_cache": {},  # in-process memo for this run - see rotowire_soccer.get_player_status
        "rotowire_match_log": [],  # every RotoWire variant-match attempt this run (item 1 name-match audit)
        "history": {},
        "history_reject_log": [],  # every history variant-match attempt this run (item 1 name-match audit)
        "espn_team_cache": {},   # (league_code, team_name) -> espn_team_id
        "espn_event_cache": {},  # (league_code, fixture_id) -> espn event or None
        "transfermarkt_cache": {},  # (league_code, team_code) -> injuries list
        "dabble_live_players": [],
    }
    try:
        context["rotowire"] = fetch_rotowire_soccer_status()
    except Exception as e:
        print(f"soccer_adapter: RotoWire feed enrichment unavailable (non-fatal): {e}")

    try:
        context["rotowire_player_index"] = rotowire_soccer.build_player_index()
        # Warm the player-page status cache for every live Dabble player
        # CONCURRENTLY up front - sequential per-player fetches inside the
        # per-candidate enrichment loop below would otherwise turn a ~76-
        # player board into 76 serial HTTP round trips (confirmed live
        # 2026-09-14 this pushed a single scoring run past 2 minutes).
        # expected_team travels with each name so a name-variant match
        # still gets team-confirmed even when prefetched (item 1).
        specs = [(p["player_name"], _expected_team_display_name(_league_code(p["league_raw"]), p["team"]))
                  for p in players.values()]
        rotowire_soccer.prefetch_player_statuses(
            specs, index=context["rotowire_player_index"], cache=context["rotowire_status_cache"],
            match_log=context["rotowire_match_log"],
        )
    except Exception as e:
        print(f"soccer_adapter: RotoWire player-page index/prefetch unavailable (non-fatal): {e}")

    try:
        context["history"] = soccer_start_history._load(soccer_start_history.HISTORY_PATH, {})
    except Exception as e:
        print(f"soccer_adapter: start-history load failed (non-fatal): {e}")

    context["dabble_live_players"] = [
        {"player_id": p.get("dabble_player_id"), "player_name": p["player_name"], "team": p["team"]}
        for p in players.values()
    ]
    return context


# ---------------------------------------------------------------------------
# Step 3: per-player enrichment (every source independent - never
# overwrites another; consensus computed separately, disagreement kept)
# ---------------------------------------------------------------------------

def _enrich_transfermarkt(player, context, league_code):
    """(hit_or_None, source_health) - resolves player["team"] (a Dabble
    board short code like "NEW") to its real club name via ESPN's own
    abbreviation list (team_display_name) BEFORE calling Transfermarkt -
    passing the bare code straight to Transfermarkt's free-text search
    returns whatever unrelated club ranks first (confirmed live
    2026-09-13: "TOR" resolved to Real Madrid, "CHI" to Liverpool - both
    silently cached as if verified in data/transfermarkt/team_
    resolution.json - see scrapers.espn_soccer.resolve_team_by_code and
    test_espn_soccer.py's regression tests guarding against a repeat).
    Cached per (league_code, team_code) since the same short code can
    mean different clubs across leagues.

    source_health separates "did we even resolve the right CLUB" (team_
    matched) from "is THIS PLAYER specifically on that club's injury
    list" (signal_found) - a matched team with no injury hit is a real,
    valuable "checked, healthy" result, not a gap (see item 6)."""
    team_matched = league_code is not None and resolve_team_by_code(league_code, player["team"]) is not None
    cache_key = (league_code, player["team"])
    if cache_key not in context["transfermarkt_cache"]:
        try:
            from soccer_dates import soccer_today_str
            today = soccer_today_str()
            full_name = team_display_name(league_code, player["team"]) if league_code else player["team"]
            context["transfermarkt_cache"][cache_key] = get_team_injuries_cached(full_name, today)
        except Exception as e:
            print(f"soccer_adapter: Transfermarkt lookup failed for {player['team']} (non-fatal): {e}")
            context["transfermarkt_cache"][cache_key] = []
    injuries = context["transfermarkt_cache"][cache_key]
    match = next((r for r in injuries if r["normalized_name"] == player["normalized_name"]), None)
    health = {"source_attempted": True, "entity_matched": team_matched, "signal_found": match is not None}
    if not match:
        return None, health
    hit = {
        "source": "transfermarkt", "section": match["section"], "reason": match["reason"],
        "since": match.get("since"), "expected_return": match.get("expected_return"),
        "expected_return_date": match.get("expected_return_date"),
    }
    return hit, health


def _enrich_rotowire(player, context, league_code):
    """(hit_or_None, source_health) - combines two independent RotoWire
    signals (item 3):
      1. the RSS headline feed (context["rotowire"]) - only covers a
         player RotoWire happened to write about within NEWS_LOOKBACK_
         HOURS, but carries a real timestamp.
      2. the player-page lookup (rotowire_soccer.get_player_status) -
         covers ANY player with a RotoWire page at all (34k+ index) via
         its live status card, but no timestamp (the page doesn't expose
         when the tag was set).
    The feed wins when both exist (more specific text, has a timestamp);
    the page fills in when the feed has nothing. entity_matched is true
    if EITHER source found this player at all - that's the coverage
    number item 3 asks for (rotowire_player_match_rate), independent of
    whether either one currently shows anything adverse (rotowire_
    adverse_signal_rate).

    expected_team (2026-09-14 item 1, name-match audit) is passed to
    get_player_status so a name-VARIANT match is team-confirmed before
    being trusted - see that function's docstring."""
    expected_team = _expected_team_display_name(league_code, player["team"])
    feed_hit = context["rotowire"].get(player["normalized_name"])
    page_result = rotowire_soccer.get_player_status(
        player["player_name"], index=context.get("rotowire_player_index"),
        cache=context.get("rotowire_status_cache"),
        use_disk_cache=context.get("rotowire_use_disk_cache", True),
        expected_team=expected_team, match_log=context.get("rotowire_match_log"),
    )

    entity_matched = feed_hit is not None or page_result["matched"]
    signal_found = feed_hit is not None or page_result["signal_found"]
    health = {"source_attempted": True, "entity_matched": entity_matched, "signal_found": signal_found,
              "player_page_matched": page_result["matched"], "player_page_url": page_result["page_url"],
              "player_page_ambiguous": page_result.get("ambiguous", False)}

    if feed_hit:
        return dict(feed_hit, rotowire_source="feed"), health
    if page_result["signal_found"]:
        # soccer_dns_score._score_dns reads rotowire_status_raw FIRST as
        # the key into ROTOWIRE_STATUS_PRIOR - for a feed hit that field
        # already holds the specific matchable tier (e.g. "fitness-test"),
        # never free display text, so a player-page hit must put its
        # MAPPED tier there too (bug found live 2026-09-14: putting the
        # page's literal tag ("GTD") there instead silently scored 0,
        # since "GTD" isn't a ROTOWIRE_STATUS_PRIOR key - the mapping to
        # "fifty-fifty" was computed but never actually reached scoring).
        # rotowire_page_tag keeps the literal page text for display/
        # --trace/dashboard purposes without touching the scoring path.
        hit = {
            "rotowire_status_raw": page_result["rotowire_status_normalized"],
            "rotowire_status_normalized": page_result["rotowire_status_normalized"],
            "rotowire_page_tag": page_result["status_tag"],
            "rotowire_injury": page_result["injury"],
            "rotowire_news_at": None,  # the player page carries no timestamp - see module docstring
            "rotowire_predicted_start": None,
            "rotowire_source": "player_page",
            # Added 2026-09-16 (Hinshelwood hard_out-floor item 2/3): the
            # page DOES expose page_url/est_return/status_since (when the
            # tag was first observed, tracked in rotowire_soccer.py's disk
            # cache) even though it has no per-story timestamp - needed
            # for the floor's conflict check and the alert research block.
            "rotowire_page_url": page_result["page_url"],
            "rotowire_est_return": page_result["est_return"],
            "rotowire_status_since": page_result.get("status_since"),
        }
        return hit, health
    return None, health


def _enrich_espn_official_status(player, context, league_code):
    """(status, squad_info, source_health) where status is 'confirmed_
    starting' | 'confirmed_not_starting' | 'not_yet_posted'. Best-effort
    team/fixture resolution - 'not_yet_posted'/health flags on any
    failure, never raises. source_health's entity_matched means "we
    resolved this player's team to a real ESPN team id" (independent of
    whether a fixture/squad has posted yet); signal_found means the
    official lineup has actually posted either way.

    HONEST CAVEAT (unverified as of this build): scrapers.espn_soccer's
    docstring says get_match_squad_names only reliably resolves once ESPN
    posts the completed match summary, not necessarily pre-kickoff. This
    function is written to work correctly whenever the data appears (pre-
    or post-release) - it does NOT assume a specific timing - but a
    genuinely pre-kickoff "official lineup out" signal for soccer is
    unproven and needs live validation against a real matchday."""
    if not league_code or league_code not in LEAGUE_SLUGS:
        return "not_yet_posted", None, {"source_attempted": False, "entity_matched": False, "signal_found": False}

    team_id = _resolve_espn_team_id(player, context, league_code)
    if not team_id:
        return "not_yet_posted", None, {"source_attempted": True, "entity_matched": False, "signal_found": False}

    event_cache_key = (league_code, player["fixture_id"] or player["event_date"])
    if event_cache_key not in context["espn_event_cache"]:
        try:
            # We only know OUR team's id reliably; find_espn_event needs
            # both sides, so find_event_by_team scans the day's
            # scoreboard for a fixture involving team_id instead of
            # requiring the opponent id too (shared with soccer_alerts.
            # grade_alerts's own need for exactly this, factored out of
            # this function's old inline duplicate 2026-09-14 item 4).
            from scrapers.espn_soccer import find_event_by_team
            date_str = player["event_date"][:10]
            context["espn_event_cache"][event_cache_key] = find_event_by_team(league_code, team_id, date_str)
        except Exception as e:
            print(f"soccer_adapter: ESPN event lookup failed (non-fatal): {e}")
            context["espn_event_cache"][event_cache_key] = None
    event = context["espn_event_cache"][event_cache_key]
    matched_health = {"source_attempted": True, "entity_matched": True, "signal_found": False}
    if not event:
        return "not_yet_posted", None, matched_health

    try:
        squad = get_match_squad_names(league_code, event["id"])
    except Exception as e:
        print(f"soccer_adapter: ESPN squad lookup failed (non-fatal): {e}")
        return "not_yet_posted", None, matched_health
    if not squad:
        return "not_yet_posted", None, matched_health

    matched_health["signal_found"] = True
    info = squad.get(player["normalized_name"])
    if info is None:
        return "confirmed_not_starting", {"reason": "not named in matchday squad"}, matched_health
    if info["starter"]:
        return "confirmed_starting", info, matched_health
    return "confirmed_not_starting", info, matched_health  # named to squad but NOT starting - grading only cares about starting XI


def _enrich_x(player, context):
    """(hit_or_None, source_health). attempted reflects whether X
    monitoring is even configured (has_credentials()) - see item 5/6:
    "no hits" while disabled must never be reported the same as "checked,
    found nothing" while enabled."""
    attempted = soccer_x_monitor.has_credentials()
    variants = soccer_x_monitor.name_variants(player["player_name"])
    hit = soccer_x_monitor.get_player_x_signal(variants) if attempted else None
    health = {
        "source_attempted": attempted, "entity_matched": hit is not None,
        "signal_found": bool(hit and (hit.get("extracted_injury") or hit.get("extracted_start_signal"))),
    }
    return hit, health


def _build_predicted_xi_sources(player, league_code, context, history, transfermarkt_hit, rotowire_hit, x_hit,
                                 expected_team_id=None):
    """[{source, vote: "start"|"bench"|"unknown", predicted_start:
    True|False|None, observed_at, source_updated_at}] across every real
    signal available - see item 4. No fabricated "predicted lineup"
    scraper exists yet (FotMob/WhoScored not built, SofaScore previously
    found blocked - see soccer_dns.py history), so this consensus is
    built from what's genuinely derivable: Transfermarkt injury status,
    RotoWire news tier, X (if configured), and recent start-pattern
    history. Documented gap, not a silent omission.

    A source that was CHECKED and found something, but that something
    doesn't clearly imply start-or-bench, is kept as vote="unknown"
    (2026-09-14 coverage-depth fix) rather than silently dropped - e.g.
    an X injury mention with no explicit start_signal, or a RotoWire hit
    whose normalized tier isn't one of the ones this module currently
    knows how to read as directional. This is what makes source_count
    (all three vote buckets) a true "how many sources actually said
    something" count, distinct from starter_votes+bench_votes."""
    now_iso = datetime.now(timezone.utc).isoformat()
    sources = []

    if transfermarkt_hit:
        sources.append({"source": "transfermarkt_injury", "vote": "bench", "predicted_start": False,
                         "observed_at": now_iso, "source_updated_at": transfermarkt_hit.get("since")})

    if rotowire_hit:
        norm_status = rotowire_hit.get("rotowire_status_normalized")
        if norm_status == "reversal":
            vote, predicted = "start", True
        elif norm_status in ("hard_out", "soft", "news_mention"):
            vote, predicted = "bench", False
        else:
            vote, predicted = "unknown", None
        sources.append({"source": "rotowire_news", "vote": vote, "predicted_start": predicted,
                         "observed_at": now_iso, "source_updated_at": rotowire_hit.get("rotowire_news_at")})

    if x_hit:
        if x_hit.get("extracted_start_signal"):
            predicted = x_hit["extracted_start_signal"] in ("confirmed_start", "returning")
            vote = "start" if predicted else "bench"
        else:
            # Matched (e.g. an injury mention) but no explicit start/bench
            # implication - a real signal, kept visible, not silently
            # dropped just because it isn't directional.
            vote, predicted = "unknown", None
        sources.append({"source": f"x:{x_hit.get('author_username') or 'unknown'}", "vote": vote,
                         "predicted_start": predicted, "observed_at": now_iso,
                         "source_updated_at": x_hit.get("created_at")})

    if history:
        reject_log = context.get("history_reject_log")
        # team_starts_last_n, not starts_last_n (2026-09-16 fix) - see
        # enrich_and_score_player's own comment on the same switch: a
        # player who has stopped appearing in squad announcements at all
        # (injured, dropped) generates no new history entries, so plain
        # starts_last_n's "last 5 RECORDED matches" can be entirely from
        # before that happened - team_starts_last_n counts a team
        # fixture the player has no entry for as a non-start instead of
        # silently skipping it.
        wins, losses, n = soccer_start_history.team_starts_last_n(
            history, player["normalized_name"], expected_team_id, n=5,
            expected_team_id=expected_team_id, reject_log=reject_log)
        if n >= 3:
            predicted = (wins / n) >= 0.6
            sources.append({"source": "recent_start_pattern", "vote": "start" if predicted else "bench",
                             "predicted_start": predicted, "observed_at": now_iso, "source_updated_at": None})
        # A player who just earned his first start after not starting is a
        # genuine "trending toward starting" signal even when the trailing
        # rate is still low - this is exactly the real Mama Balde case
        # (0 starts, then broke into the XI last match) found during the
        # 2026-09-14 audit. Kept as its OWN source (not folded into the
        # rate-based one above) so both can coexist/disagree honestly.
        if soccer_start_history.first_recent_start(
                history, player["normalized_name"], expected_team_id=expected_team_id, reject_log=reject_log):
            sources.append({"source": "first_recent_start", "vote": "start", "predicted_start": True,
                             "observed_at": now_iso, "source_updated_at": None})

    return sources


def enrich_and_score_player(player, context):
    league_code = _league_code(player["league_raw"])
    official_status, squad_info, official_health = _enrich_espn_official_status(player, context, league_code)
    transfermarkt_hit, transfermarkt_health = _enrich_transfermarkt(player, context, league_code)
    rotowire_hit, rotowire_health = _enrich_rotowire(player, context, league_code)
    x_hit, x_health = _enrich_x(player, context)
    history = context["history"]
    # The same ESPN team id _enrich_espn_official_status resolves (cached,
    # so free here) - required to confirm any history name-VARIANT match
    # (item 1, 2026-09-14 name-match audit); a missing/unresolved team id
    # means a variant match can't be confirmed and is rejected, not a
    # silent name-only accept.
    espn_team_id = _resolve_espn_team_id(player, context, league_code)
    history_reject_log = context.get("history_reject_log")

    predicted_xi_sources = _build_predicted_xi_sources(
        player, league_code, context, history, transfermarkt_hit, rotowire_hit, x_hit,
        expected_team_id=espn_team_id)
    predicted_start_sources = [s["source"] for s in predicted_xi_sources if s["predicted_start"] is True]
    predicted_bench_sources = [s["source"] for s in predicted_xi_sources if s["predicted_start"] is False]
    predicted_unknown_sources = [s["source"] for s in predicted_xi_sources if s["vote"] == "unknown"]
    if predicted_start_sources and predicted_bench_sources:
        prediction_consensus = "disagreement"
    elif predicted_start_sources:
        prediction_consensus = "start"
    elif predicted_bench_sources:
        prediction_consensus = "bench"
    else:
        prediction_consensus = "unknown"
    prediction_disagreement = bool(predicted_start_sources) and bool(predicted_bench_sources)

    # Explicit vote-count structure (2026-09-14 item 4) - same underlying
    # data as predicted_start_sources/predicted_bench_sources/prediction_
    # consensus above (kept for backward compat with soccer_dns_score.py
    # and existing callers - scoring is UNCHANGED, this is display/
    # coverage-only), but named the way a dashboard coverage matrix and
    # "how many sources actually said something" question wants it.
    starter_votes = len(predicted_start_sources)
    bench_votes = len(predicted_bench_sources)
    unknown_votes = len(predicted_unknown_sources)
    predicted_xi_source_count = len(predicted_xi_sources)

    _h_kwargs = {"expected_team_id": espn_team_id, "reject_log": history_reject_log}
    # team_starts_last_n, not starts_last_n (2026-09-16 fix) - found live
    # (Jack Hinshelwood, Brighton, injured since 2026-08-26): a player
    # who stops appearing in squad announcements at all generates no new
    # history entries, so plain starts_last_n's "last N RECORDED
    # matches" can be entirely from before an injury - Hinshelwood's
    # showed 100% starts / 0% non-start rate using 5 matches that all
    # predated his injury, since ESPN's squad list simply never
    # mentions an absent player at all (no entry recorded, not "started:
    # False"). team_starts_last_n instead counts the TEAM's real last N
    # fixtures - one this player has no entry for (injured, dropped, not
    # named to the squad) counts as a non-start, using OTHER players on
    # the same team_id to derive the team's actual fixture list.
    starts5 = soccer_start_history.team_starts_last_n(
        history, player["normalized_name"], espn_team_id, n=5, **_h_kwargs) if history else (0, 0, 0)
    starts10 = soccer_start_history.team_starts_last_n(
        history, player["normalized_name"], espn_team_id, n=10, **_h_kwargs) if history else (0, 0, 0)
    days_rest_val = soccer_start_history.days_rest(history, player["normalized_name"], player["event_date"][:10], **_h_kwargs) if history else None
    first_start = soccer_start_history.first_recent_start(history, player["normalized_name"], **_h_kwargs) if history else False
    freq_sub = soccer_start_history.frequent_substitute(history, player["normalized_name"], **_h_kwargs) if history else False
    rotation = soccer_start_history.rotation_player(history, player["normalized_name"], **_h_kwargs) if history else False
    prev_start = soccer_start_history.previous_start(history, player["normalized_name"], **_h_kwargs) if history else None
    starts3 = soccer_start_history.team_starts_last_3(
        history, player["normalized_name"], espn_team_id, **_h_kwargs) if history else (0, 0, 0)

    # RotoWire hard_out floor inputs (2026-09-16, Hinshelwood item 2) -
    # computed here (soccer_adapter.py has the history/transfermarkt data
    # this needs) and passed into the pure scorer as two simple facts:
    # is a second source corroborating, and is there a live conflict.
    hard_out_corroborated = False
    hard_out_conflict = None
    corroboration_sources = []
    if rotowire_hit and rotowire_hit.get("rotowire_status_raw") == "hard_out":
        if transfermarkt_hit is not None:
            corroboration_sources.append("Transfermarkt injury list")
        if history and soccer_start_history.absent_from_most_recent_team_fixture(
                history, player["normalized_name"], espn_team_id, **_h_kwargs):
            corroboration_sources.append("absent from most recent squad")
        hard_out_corroborated = bool(corroboration_sources)

        status_since = rotowire_hit.get("rotowire_status_since")
        last_start_date = soccer_start_history.most_recent_start_date(
            history, player["normalized_name"], **_h_kwargs) if history else None
        if status_since and last_start_date:
            try:
                since_date = datetime.fromisoformat(status_since).date().isoformat()
            except Exception:
                since_date = None
            if since_date and last_start_date > since_date:
                hard_out_conflict = (
                    f"started {last_start_date} after hard_out first seen {since_date}")

    # Research block (2026-09-16 item 3) - the detail behind a hard_out
    # alert, for the Discord embed and dashboard to show without either
    # one re-deriving it: RotoWire's own page state, a Transfermarkt
    # entry if one exists, when he was last actually on the pitch, and
    # the team's last 3 fixtures broken down start/bench/absent. Only
    # built for hard_out (item 3 is explicitly scoped to "hard_out
    # players") - None for every other candidate, cheap either way.
    research_block = None
    if rotowire_hit and rotowire_hit.get("rotowire_status_raw") == "hard_out":
        last_appeared = soccer_start_history.last_appearance_date(
            history, player["normalized_name"], **_h_kwargs) if history else None
        days_since_last_appearance = None
        if last_appeared:
            try:
                days_since_last_appearance = (
                    datetime.strptime(player["event_date"][:10], "%Y-%m-%d")
                    - datetime.strptime(last_appeared, "%Y-%m-%d")).days
            except Exception:
                days_since_last_appearance = None
        research_block = {
            "rotowire_url": rotowire_hit.get("rotowire_page_url"),
            "rotowire_status_tag": rotowire_hit.get("rotowire_page_tag"),
            "rotowire_injury": rotowire_hit.get("rotowire_injury"),
            "rotowire_est_return": rotowire_hit.get("rotowire_est_return"),
            "rotowire_status_since": rotowire_hit.get("rotowire_status_since"),
            "transfermarkt": ({
                "reason": transfermarkt_hit["reason"], "since": transfermarkt_hit.get("since"),
                "expected_return": transfermarkt_hit.get("expected_return"),
            } if transfermarkt_hit else None),
            "last_appeared_date": last_appeared,
            "days_since_last_appearance": days_since_last_appearance,
            "team_last_3_fixtures": soccer_start_history.team_fixture_detail(
                history, player["normalized_name"], espn_team_id, n=3, **_h_kwargs) if history else [],
            "corroborated_by": corroboration_sources,
            "conflict": hard_out_conflict,
        }

    dns_score, confidence_score, urgency_score, evidence_count, contributions, urgency_reasons = \
        soccer_dns_score.score_candidate(
            official_status=official_status,
            transfermarkt_hit=transfermarkt_hit,
            rotowire_hit=rotowire_hit,
            x_hit=x_hit,
            starts_last_5=starts5,
            starts_last_10=starts10,
            first_recent_start=first_start,
            frequent_substitute=freq_sub,
            rotation_player=rotation,
            predicted_start_sources=predicted_start_sources,
            predicted_bench_sources=predicted_bench_sources,
            event_date=player["event_date"],
            hard_out_corroborated=hard_out_corroborated,
            hard_out_conflict=hard_out_conflict,
        )

    # LOCK tier (2026-09-17, item 1) - a candidate with a live Dabble
    # prop (every scored candidate here, by construction - see the
    # module docstring: the Dabble board IS the candidate universe) AND
    # either an official confirmed-not-starting lineup OR an
    # uncontradicted RotoWire hard_out ranks above every additive-score
    # candidate, regardless of competition/league/history coverage - the
    # whole point being that neither of those two facts needs a score to
    # be trustworthy. A hard_out WITH a live conflict (started after the
    # tag was first seen) is explicitly excluded - see hard_out_conflict
    # above - since the tag itself is in doubt there, not confirmed
    # evidence. Ranking/ordering within LOCK lives in soccer_dns_score.
    # lock_aware_sort_key, not here - this only decides membership.
    is_lock = False
    lock_reason = None
    if official_status == "confirmed_not_starting":
        is_lock = True
        lock_reason = "confirmed_not_starting"
    elif (rotowire_hit and rotowire_hit.get("rotowire_status_raw") == "hard_out"
          and not hard_out_conflict):
        is_lock = True
        lock_reason = "hard_out_corroborated" if hard_out_corroborated else "hard_out_uncorroborated"

    # Watchdog: never silently drop a candidate because enrichment failed -
    # see item 10. A player with zero usable enrichment signals is still
    # scored (on history/prior alone) and explicitly flagged. low_data is
    # about the SCORE's evidentiary weight (dnp_score.py's confidence
    # philosophy) - see the has_*/source_health fields below for the
    # separate, unambiguous COVERAGE question the 2026-09-14 audit asked
    # for (item 1): "did every source get a fair, checked attempt", not
    # "how much did that attempt end up adding to the score."
    low_data = evidence_count <= 1

    # --- Coverage metrics (2026-09-14 coverage audit, item 1) - explicit,
    # reconciled definitions instead of overloading evidence_count/
    # low_data (whose >=3-match significance floor made "history
    # coverage" and "completely unenriched" contradict each other in the
    # original report). has_history here means "ANY recorded match at
    # all" (n>=1) - a real, if thin, coverage fact - separate from
    # whether that sample was large enough to move the score. ----------
    has_history = starts5[2] > 0 or starts10[2] > 0
    has_predicted_xi = bool(predicted_xi_sources)
    has_injury = transfermarkt_hit is not None
    has_news = rotowire_hit is not None
    has_x_signal = x_hit is not None
    # The four INDEPENDENT raw channels - has_predicted_xi is deliberately
    # excluded from this count: it's a synthesis OF these four (see
    # _build_predicted_xi_sources), so counting it here would double-count
    # the same evidence as a second "source."
    _raw_source_count = sum([has_history, has_injury, has_news, has_x_signal])
    has_any_external_enrichment = _raw_source_count > 0
    has_multiple_external_sources = _raw_source_count >= 2
    board_only = not has_any_external_enrichment  # item 1's "completely_unenriched"

    source_health = {
        "official_lineup": official_health,
        "transfermarkt": transfermarkt_health,
        "rotowire": rotowire_health,
        "x": x_health,
        "history": {"source_attempted": True, "entity_matched": has_history, "signal_found": None},
    }

    return {
        "player_name": player["player_name"], "normalized_name": player["normalized_name"],
        "dabble_player_id": player["dabble_player_id"], "team": player["team"],
        "league_raw": player["league_raw"], "league_code": league_code,
        "matchup": player["matchup"], "event_date": player["event_date"],
        "fixture_id": player["fixture_id"], "position": player["position"],
        "props": player["props"],
        "dabble_status_raw": player["dabble_status_raw"],
        "dabble_status_normalized": player["dabble_status_normalized"],
        "official_status": official_status,
        "transfermarkt_injury": transfermarkt_hit,
        "rotowire_status_raw": (rotowire_hit or {}).get("rotowire_status_raw"),
        "rotowire_status_normalized": (rotowire_hit or {}).get("rotowire_status_normalized"),
        # The literal tag text as RotoWire's OWN page displays it (e.g.
        # "GTD") - always populated when a player-page hit exists, even
        # if that tag didn't map to a scoreable tier (see _enrich_rotowire's
        # comment) - use THIS for human-facing display, never rotowire_
        # status_raw/normalized, which are scoring-oriented fields that
        # can be None for an unmapped-but-real tag.
        "rotowire_page_tag": (rotowire_hit or {}).get("rotowire_page_tag"),
        "rotowire_injury": (rotowire_hit or {}).get("rotowire_injury"),
        "rotowire_news_at": (rotowire_hit or {}).get("rotowire_news_at"),
        "rotowire_predicted_start": (rotowire_hit or {}).get("rotowire_predicted_start"),
        # Added 2026-09-16 (Hinshelwood items 2/3): the research block and
        # dashboard need the page link/est. return/first-seen timestamp,
        # not just the scoring-oriented status fields above.
        "rotowire_page_url": (rotowire_hit or {}).get("rotowire_page_url"),
        "rotowire_est_return": (rotowire_hit or {}).get("rotowire_est_return"),
        "rotowire_status_since": (rotowire_hit or {}).get("rotowire_status_since"),
        "hard_out_corroborated": hard_out_corroborated,
        "hard_out_corroboration_sources": corroboration_sources,
        "hard_out_conflict": hard_out_conflict,
        "hard_out_research_block": research_block,
        "is_lock": is_lock,
        "lock_reason": lock_reason,
        "x_signal": x_hit,
        "starts_last_5": starts5, "starts_last_10": starts10, "days_rest": days_rest_val,
        "first_recent_start": first_start, "frequent_substitute": freq_sub, "rotation_player": rotation,
        "previous_start": prev_start, "starts_last_3": starts3,
        "predicted_start_sources": predicted_start_sources,
        "predicted_bench_sources": predicted_bench_sources,
        "predicted_unknown_sources": predicted_unknown_sources,
        "prediction_consensus": prediction_consensus,
        "prediction_disagreement": prediction_disagreement,
        "predicted_xi_sources": predicted_xi_sources,
        "starter_votes": starter_votes,
        "bench_votes": bench_votes,
        "unknown_votes": unknown_votes,
        "predicted_xi_source_count": predicted_xi_source_count,
        "dns_score": dns_score, "confidence_score": confidence_score, "urgency_score": urgency_score,
        "combined_priority": soccer_dns_score.combined_priority(dns_score, urgency_score),
        "evidence_count": evidence_count,
        "tier": soccer_dns_score.tier_label(dns_score),
        "top_reasons": [label for label, _ in contributions],
        "urgency_reasons": urgency_reasons,
        "contributions": contributions,
        "low_data": low_data,
        "watchdog_note": "LOW DATA / ENRICHMENT MISSING" if low_data else None,
        "rotowire_source": (rotowire_hit or {}).get("rotowire_source"),
        "rotowire_player_page_matched": rotowire_health.get("player_page_matched", False),
        "rotowire_player_page_url": rotowire_health.get("player_page_url"),
        "has_history": has_history,
        "has_predicted_xi": has_predicted_xi,
        "has_injury": has_injury,
        "has_news": has_news,
        "has_x_signal": has_x_signal,
        "has_any_external_enrichment": has_any_external_enrichment,
        "has_multiple_external_sources": has_multiple_external_sources,
        "board_only": board_only,
        "source_health": source_health,
    }


NAME_MATCH_AUDIT_PATH = os.path.join("data", "soccer_name_match_audit.json")


def _summarize_match_log(entries):
    accepted = sum(1 for e in entries if e["accepted"])
    total = len(entries)
    return {"total_attempts": total, "accepted": accepted, "rejected": total - accepted,
            "precision": round(100 * accepted / total, 1) if total else None, "entries": entries}


def _write_name_match_audit(context, date_str):
    """Persists every name-VARIANT match attempt (history + RotoWire)
    this run made, accepted or rejected, with a precision summary -
    2026-09-14 item 1's name-match audit needs this to inspect real
    matches without re-running/re-fetching. Written every run (not just
    when something was rejected) so precision=None ("nothing to check
    this run") is visibly distinct from "checked and all passed."."""
    audit = {
        "generated_at": datetime.now(timezone.utc).isoformat(), "date": date_str,
        "history": _summarize_match_log(context.get("history_reject_log", [])),
        "rotowire": _summarize_match_log(context.get("rotowire_match_log", [])),
    }
    os.makedirs(os.path.dirname(NAME_MATCH_AUDIT_PATH) or ".", exist_ok=True)
    with open(NAME_MATCH_AUDIT_PATH, "w", encoding="utf-8") as f:
        json.dump(audit, f, indent=2, ensure_ascii=False)
    return audit


def score_dabble_soccer_board(source=DEFAULT_BOARD_PATH, persist=True, snapshot_date=None, notify=True):
    from soccer_dates import soccer_today_str
    snapshot_date = snapshot_date or soccer_today_str()
    props = load_dabble_soccer_props(source)
    players = group_props_by_player(props)
    if not players:
        print("soccer_adapter: no soccer players found on this board.")
        return []

    context = _build_enrichment_context(players)
    candidates = [enrich_and_score_player(p, context) for p in players.values()]
    # LOCK-aware ordering (2026-09-17, item 1) - a LOCK candidate (an
    # uncontradicted hard_out or an official confirmed-not-starting
    # lineup) ranks above every additive-score candidate here, so every
    # downstream consumer of this list (print_ranked, the alert loop,
    # anything that doesn't re-sort) inherits LOCK-first order for free;
    # soccer_dashboard.py and soccer_daily_digest.py each do their own
    # re-sort and use the same key.
    candidates.sort(key=soccer_dns_score.lock_aware_sort_key)

    if persist:
        record_live_snapshot(snapshot_date, candidates)
        try:
            _write_name_match_audit(context, snapshot_date)
        except Exception as e:
            print(f"soccer_adapter: name-match audit write failed (non-fatal): {e}")

    if notify:
        try:
            import soccer_alerts
            soccer_alerts.notify_soccer_candidates(candidates, date_str=snapshot_date)
        except Exception as e:
            print(f"soccer_adapter: soccer_alerts.notify_soccer_candidates failed (non-fatal): {e}")

    return candidates


# ---------------------------------------------------------------------------
# Live snapshot persistence - same pattern as MLB's dabble_adapter.py
# ---------------------------------------------------------------------------

def _snapshot_key(candidate):
    if candidate.get("dabble_player_id"):
        return f"pid:{candidate['dabble_player_id']}"
    return f"name:{candidate['normalized_name']}:{candidate['team']}"


def record_live_snapshot(date_str, candidates):
    os.makedirs(LIVE_SNAPSHOTS_DIR, exist_ok=True)
    path = os.path.join(LIVE_SNAPSHOTS_DIR, f"{date_str}.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    now_iso = datetime.now(timezone.utc).isoformat()
    data["_latest_batch_ts"] = now_iso
    for c in candidates:
        key = _snapshot_key(c)
        entry = data.setdefault(key, {
            "name": c["player_name"], "team": c["team"], "dabble_player_id": c.get("dabble_player_id"),
            "fixture_id": c.get("fixture_id"),
            "score_history": [], "first_crossed": {str(t): None for t in THRESHOLDS_TO_TRACK},
        })
        entry["name"] = c["player_name"]
        entry["dabble_player_id"] = c.get("dabble_player_id")
        if c.get("fixture_id"):
            entry["fixture_id"] = c["fixture_id"]
        entry["score_history"].append({
            "ts": now_iso, "dns_score": c["dns_score"], "confidence_score": c["confidence_score"],
            "urgency_score": c["urgency_score"], "official_status": c["official_status"],
            "dabble_status_raw": c.get("dabble_status_raw"),
        })
        for t in THRESHOLDS_TO_TRACK:
            if c["dns_score"] >= t and entry["first_crossed"][str(t)] is None:
                entry["first_crossed"][str(t)] = now_iso
        entry["latest"] = {k: v for k, v in c.items() if k not in ("props",)}
        entry["latest"]["ts"] = now_iso
        entry["latest"]["props"] = [{"market": p["market"], "line": p["line"], "prop_id": p["prop_id"]}
                                     for p in c.get("props", [])]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return data


def print_ranked(candidates, top_n=25):
    shown = candidates[:top_n]
    if not shown:
        print("No candidates.")
        return
    print(f"{'DNS':<5}{'Conf':<6}{'Urg':<5}{'Player':<22}{'Match':<20}{'Dabble':<10}{'Injury':<14}"
          f"{'Predicted XI':<16}{'Live':<6}Top reasons")
    for c in shown:
        matchup = c.get("matchup") or c["team"]
        dabble_status = c.get("dabble_status_normalized") or c.get("dabble_status_raw") or "n/a"
        injury = (c.get("transfermarkt_injury") or {}).get("reason") or c.get("rotowire_status_raw") or "-"
        xi = c["prediction_consensus"] + (f" ({len(c['predicted_start_sources'])}/{len(c['predicted_xi_sources'])})"
                                            if c["predicted_xi_sources"] else "")
        reasons = "; ".join(c["top_reasons"][:2])
        print(f"{c['dns_score']:<5}{c['confidence_score']:<6}{c['urgency_score']:<5}{c['player_name']:<22}"
              f"{matchup[:19]:<20}{str(dabble_status)[:9]:<10}{str(injury)[:13]:<14}{xi[:15]:<16}{'LIVE':<6}{reasons}")


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # see soccer_dns.main's comment
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Score the live Dabble soccer board for DNS risk")
    parser.add_argument("--source", default=DEFAULT_BOARD_PATH)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()

    candidates = score_dabble_soccer_board(source=args.source, persist=not args.no_persist,
                                            notify=not args.no_notify)
    print(f"\nScored {len(candidates)} soccer players from {args.source}.\n")
    print_ranked(candidates, top_n=args.top)


if __name__ == "__main__":
    main()
