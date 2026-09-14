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

import soccer_dns_score
import soccer_start_history
import soccer_x_monitor
from board_loader import _LEAGUE_VALUE_TO_CANONICAL
from rotowire_soccer import fetch_rotowire_soccer_status
from scrapers.betr import normalize_name
from scrapers.espn_soccer import (match_espn_team_id, find_espn_event, get_match_squad_names,
                                   team_display_name, LEAGUE_SLUGS)
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


def _build_enrichment_context(players):
    context = {
        "rotowire": {},
        "history": {},
        "espn_team_cache": {},   # (league_code, team_name) -> espn_team_id
        "espn_event_cache": {},  # (league_code, fixture_id) -> espn event or None
        "transfermarkt_cache": {},  # team full name -> injuries list
        "dabble_live_players": [],
    }
    try:
        context["rotowire"] = fetch_rotowire_soccer_status()
    except Exception as e:
        print(f"soccer_adapter: RotoWire enrichment unavailable (non-fatal): {e}")

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
    """Resolves player["team"] (a Dabble board short code like "NEW") to
    its real club name via ESPN's own abbreviation list (team_display_
    name) BEFORE calling Transfermarkt - passing the bare code straight
    to Transfermarkt's free-text search returns whatever unrelated club
    ranks first (confirmed live 2026-09-13: "TOR" resolved to Real
    Madrid, "CHI" to Liverpool - both silently cached as if verified in
    data/transfermarkt/team_resolution.json). Cached per (league_code,
    team_code) since the same short code can mean different clubs across
    leagues; falls back to the raw code (old, unsafe behavior) only when
    no league_code is available to resolve against at all."""
    cache_key = (league_code, player["team"])
    if cache_key not in context["transfermarkt_cache"]:
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            full_name = team_display_name(league_code, player["team"]) if league_code else player["team"]
            context["transfermarkt_cache"][cache_key] = get_team_injuries_cached(full_name, today)
        except Exception as e:
            print(f"soccer_adapter: Transfermarkt lookup failed for {player['team']} (non-fatal): {e}")
            context["transfermarkt_cache"][cache_key] = []
    injuries = context["transfermarkt_cache"][cache_key]
    match = next((r for r in injuries if r["normalized_name"] == player["normalized_name"]), None)
    if not match:
        return None
    return {
        "source": "transfermarkt", "section": match["section"], "reason": match["reason"],
        "since": match.get("since"), "expected_return": match.get("expected_return"),
        "expected_return_date": match.get("expected_return_date"),
    }


def _enrich_rotowire(player, context):
    return context["rotowire"].get(player["normalized_name"])


def _enrich_espn_official_status(player, context, league_code):
    """(status, squad_info) where status is 'confirmed_starting' |
    'confirmed_not_starting' | 'not_yet_posted'. Best-effort team/fixture
    resolution - None/'not_yet_posted' on any failure, never raises.

    HONEST CAVEAT (unverified as of this build): scrapers.espn_soccer's
    docstring says get_match_squad_names only reliably resolves once ESPN
    posts the completed match summary, not necessarily pre-kickoff. This
    function is written to work correctly whenever the data appears (pre-
    or post-release) - it does NOT assume a specific timing - but a
    genuinely pre-kickoff "official lineup out" signal for soccer is
    unproven and needs live validation against a real matchday."""
    if not league_code or league_code not in LEAGUE_SLUGS:
        return "not_yet_posted", None

    team_cache_key = (league_code, player["team"])
    if team_cache_key not in context["espn_team_cache"]:
        try:
            context["espn_team_cache"][team_cache_key] = match_espn_team_id(league_code, player["team"])
        except Exception:
            context["espn_team_cache"][team_cache_key] = None
    team_id = context["espn_team_cache"][team_cache_key]
    if not team_id:
        return "not_yet_posted", None

    event_cache_key = (league_code, player["fixture_id"] or player["event_date"])
    if event_cache_key not in context["espn_event_cache"]:
        try:
            date_str = player["event_date"][:10]
            # We only know OUR team's id reliably; find_espn_event needs
            # both sides, so scan the day's scoreboard for a fixture
            # involving team_id instead of requiring the opponent id too.
            from scrapers.espn_soccer import get_scoreboard
            event = None
            for offset in (0, 1, -1):
                d = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=offset)).strftime("%Y-%m-%d")
                for ev in get_scoreboard(league_code, d):
                    comp = (ev.get("competitions") or [{}])[0]
                    ids = {c.get("team", {}).get("id") for c in comp.get("competitors", [])}
                    if team_id in ids:
                        event = ev
                        break
                if event:
                    break
            context["espn_event_cache"][event_cache_key] = event
        except Exception as e:
            print(f"soccer_adapter: ESPN event lookup failed (non-fatal): {e}")
            context["espn_event_cache"][event_cache_key] = None
    event = context["espn_event_cache"][event_cache_key]
    if not event:
        return "not_yet_posted", None

    try:
        squad = get_match_squad_names(league_code, event["id"])
    except Exception as e:
        print(f"soccer_adapter: ESPN squad lookup failed (non-fatal): {e}")
        return "not_yet_posted", None
    if not squad:
        return "not_yet_posted", None

    info = squad.get(player["normalized_name"])
    if info is None:
        return "confirmed_not_starting", {"reason": "not named in matchday squad"}
    if info["starter"]:
        return "confirmed_starting", info
    return "confirmed_not_starting", info  # named to squad but NOT starting - grading only cares about starting XI


def _enrich_x(player, context):
    variants = soccer_x_monitor.name_variants(player["player_name"])
    return soccer_x_monitor.get_player_x_signal(variants)


def _build_predicted_xi_sources(player, league_code, context, history, transfermarkt_hit, rotowire_hit, x_hit):
    """[{source, predicted_start: bool, observed_at, source_updated_at}]
    across every real signal available - see item 4. No fabricated
    "predicted lineup" scraper exists yet (FotMob/WhoScored not built,
    SofaScore previously found blocked - see soccer_dns.py history), so
    this consensus is built from what's genuinely derivable: Transfermarkt
    injury status, RotoWire news tier, X (if configured), and recent
    start-pattern history. Documented gap, not a silent omission."""
    now_iso = datetime.now(timezone.utc).isoformat()
    sources = []

    if transfermarkt_hit:
        sources.append({"source": "transfermarkt_injury", "predicted_start": False,
                         "observed_at": now_iso, "source_updated_at": transfermarkt_hit.get("since")})

    if rotowire_hit:
        norm_status = rotowire_hit.get("rotowire_status_normalized")
        if norm_status == "reversal":
            predicted = True
        elif norm_status in ("hard_out", "soft", "news_mention"):
            predicted = False
        else:
            predicted = None
        if predicted is not None:
            sources.append({"source": "rotowire_news", "predicted_start": predicted,
                             "observed_at": now_iso, "source_updated_at": rotowire_hit.get("rotowire_news_at")})

    if x_hit and x_hit.get("extracted_start_signal"):
        predicted = x_hit["extracted_start_signal"] in ("confirmed_start", "returning")
        sources.append({"source": f"x:{x_hit.get('author_username') or 'unknown'}", "predicted_start": predicted,
                         "observed_at": now_iso, "source_updated_at": x_hit.get("created_at")})

    if history:
        wins, losses, n = soccer_start_history.starts_last_n(history, player["normalized_name"], n=5)
        if n >= 3:
            sources.append({"source": "recent_start_pattern", "predicted_start": (wins / n) >= 0.6,
                             "observed_at": now_iso, "source_updated_at": None})
        # A player who just earned his first start after not starting is a
        # genuine "trending toward starting" signal even when the trailing
        # rate is still low - this is exactly the real Mama Balde case
        # (0 starts, then broke into the XI last match) found during the
        # 2026-09-14 audit. Kept as its OWN source (not folded into the
        # rate-based one above) so both can coexist/disagree honestly.
        if soccer_start_history.first_recent_start(history, player["normalized_name"]):
            sources.append({"source": "first_recent_start", "predicted_start": True,
                             "observed_at": now_iso, "source_updated_at": None})

    return sources


def enrich_and_score_player(player, context):
    league_code = _league_code(player["league_raw"])
    official_status, squad_info = _enrich_espn_official_status(player, context, league_code)
    transfermarkt_hit = _enrich_transfermarkt(player, context, league_code)
    rotowire_hit = _enrich_rotowire(player, context)
    x_hit = _enrich_x(player, context)
    history = context["history"]

    predicted_xi_sources = _build_predicted_xi_sources(
        player, league_code, context, history, transfermarkt_hit, rotowire_hit, x_hit)
    predicted_start_sources = [s["source"] for s in predicted_xi_sources if s["predicted_start"] is True]
    predicted_bench_sources = [s["source"] for s in predicted_xi_sources if s["predicted_start"] is False]
    if predicted_start_sources and predicted_bench_sources:
        prediction_consensus = "disagreement"
    elif predicted_start_sources:
        prediction_consensus = "start"
    elif predicted_bench_sources:
        prediction_consensus = "bench"
    else:
        prediction_consensus = "unknown"
    prediction_disagreement = bool(predicted_start_sources) and bool(predicted_bench_sources)

    starts5 = soccer_start_history.starts_last_n(history, player["normalized_name"], n=5) if history else (0, 0, 0)
    starts10 = soccer_start_history.starts_last_n(history, player["normalized_name"], n=10) if history else (0, 0, 0)
    days_rest_val = soccer_start_history.days_rest(history, player["normalized_name"], player["event_date"][:10]) if history else None
    first_start = soccer_start_history.first_recent_start(history, player["normalized_name"]) if history else False
    freq_sub = soccer_start_history.frequent_substitute(history, player["normalized_name"]) if history else False
    rotation = soccer_start_history.rotation_player(history, player["normalized_name"]) if history else False

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
        )

    # Watchdog: never silently drop a candidate because enrichment failed -
    # see item 10. A player with zero usable enrichment signals is still
    # scored (on history/prior alone) and explicitly flagged.
    enrichment_sources_found = sum([
        bool(league_code), bool(transfermarkt_hit is not None or (league_code is not None)),
        bool(rotowire_hit), bool(x_hit), starts5[2] > 0,
    ])
    low_data = evidence_count <= 1

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
        "rotowire_injury": (rotowire_hit or {}).get("rotowire_injury"),
        "rotowire_news_at": (rotowire_hit or {}).get("rotowire_news_at"),
        "rotowire_predicted_start": (rotowire_hit or {}).get("rotowire_predicted_start"),
        "x_signal": x_hit,
        "starts_last_5": starts5, "starts_last_10": starts10, "days_rest": days_rest_val,
        "first_recent_start": first_start, "frequent_substitute": freq_sub, "rotation_player": rotation,
        "predicted_start_sources": predicted_start_sources,
        "predicted_bench_sources": predicted_bench_sources,
        "prediction_consensus": prediction_consensus,
        "prediction_disagreement": prediction_disagreement,
        "predicted_xi_sources": predicted_xi_sources,
        "dns_score": dns_score, "confidence_score": confidence_score, "urgency_score": urgency_score,
        "combined_priority": soccer_dns_score.combined_priority(dns_score, urgency_score),
        "evidence_count": evidence_count,
        "tier": soccer_dns_score.tier_label(dns_score),
        "top_reasons": [label for label, _ in contributions],
        "urgency_reasons": urgency_reasons,
        "contributions": contributions,
        "low_data": low_data,
        "watchdog_note": "LOW DATA / ENRICHMENT MISSING" if low_data else None,
    }


def score_dabble_soccer_board(source=DEFAULT_BOARD_PATH, persist=True, snapshot_date=None, notify=True):
    snapshot_date = snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    props = load_dabble_soccer_props(source)
    players = group_props_by_player(props)
    if not players:
        print("soccer_adapter: no soccer players found on this board.")
        return []

    context = _build_enrichment_context(players)
    candidates = [enrich_and_score_player(p, context) for p in players.values()]
    candidates.sort(key=lambda c: c["dns_score"], reverse=True)

    if persist:
        record_live_snapshot(snapshot_date, candidates)

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
