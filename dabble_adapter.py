"""
Dabble MLB board adapter - Phase 2 of the DNP score (see dnp_score.py).

pipeline.py's data/{date}.json only ever contains the 9 players pipeline.py
itself guessed into a lineup slot - a player Dabble has a live prop on but
whose team's real (or even projected) lineup pipeline.py never constructed
is invisible to dnp_score.score_today() entirely. This module makes the
Dabble board ITSELF the source of the player universe: every player with a
live MLB prop gets resolved, enriched, and scored, independent of what
pipeline.py guessed.

Pipeline:
  load_dabble_props()      - raw board JSON -> deduped per-PROP records
  group_props_by_player()  - per-prop records -> one record per player
  score_dabble_board()     - resolves game/lineup/pitcher/history context
                              ONCE per slate, enriches every player, scores
                              them via dnp_score.score_candidate() (the same
                              core scorer + same weights the pipeline path
                              uses - see dnp_score.py's module docstring),
                              persists a timestamped live snapshot, and
                              returns the ranked list.

Usage:
  python dabble_adapter.py                     # score the live board, print top 20
  python dabble_adapter.py --source path.json  # score a specific board file
"""

import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import dnp_score
import start_history
from board_loader import _split_matchup, PITCHER_POSITIONS, PITCHER_ONLY_MARKETS
from scrapers.betr import normalize_name
from scrapers.mlb_api import get_games, get_team_abbreviations, get_active_roster, get_game_start_data
from stale_lines import _find_game_for_team, _build_lineup_index, _parse_iso, _dedupe_games

DEFAULT_BOARD_PATH = os.path.join("data", "dns_watch", "processed", "mlb", "dabble_mlb_board.json")
LIVE_SNAPSHOTS_DIR = os.path.join("data", "dnp_live_snapshots")

# Dabble's own export already uses MLB's team-abbreviation conventions
# (AZ, CWS - verified against a real board 2026-09-13), unlike Betr's
# export which needed BETR_TEAM_ABBR_OVERRIDES (ARI/CHW). Kept as an empty
# dict, same pattern as that override table, in case Dabble's abbreviations
# ever drift from MLB's.
DABBLE_TEAM_ABBR_OVERRIDES = {}

THRESHOLDS_TO_TRACK = [60, 70, 80, 90]


# ---------------------------------------------------------------------------
# Step 1: raw board -> deduped per-prop records
# ---------------------------------------------------------------------------

def _prop_id(normalized_name, team, market, line):
    """Synthesized, deterministic ID - Dabble's real export (as bridged in
    by dabble_dns_local.py) carries no prop-level id field at all, only
    player/team/market/line/event_date. Deterministic on those fields so
    the exact same prop reported twice (e.g. the same file processed twice,
    or a duplicate row from a scraper glitch) always collapses to the same
    id rather than being double-counted."""
    raw = f"{normalized_name}:{team}:{market}:{line}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def load_dabble_props(source=DEFAULT_BOARD_PATH, scraped_at=None):
    """Parse a Dabble board (file path, or an already-parsed dict/list -
    for tests/programmatic use) into per-prop dicts. Independent of
    board_loader.PropRecord: that dataclass has no prop_id/scraped_at
    concept and drops the raw "matchup" string once parsed, which MLB
    needs (unlike soccer) to resolve opponent/game - see
    _resolve_player_context. Reuses board_loader._split_matchup and
    scrapers.betr.normalize_name rather than reimplementing either.

    Deliberately tolerant of a malformed individual record (skips it) -
    this reads a live automated export on a polling cadence, not a
    hand-authored file where board_loader.load_prop_records' loud-failure
    default is the right call."""
    if isinstance(source, (str, os.PathLike)):
        with open(source, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = source

    if isinstance(data, dict):
        props_raw = data.get("props", [])
        file_scraped_at = data.get("generated_at")
    elif isinstance(data, list):
        props_raw = data
        file_scraped_at = None
    else:
        raise ValueError("Dabble board must be a dict with 'props' or a bare list.")

    scraped_at = scraped_at or file_scraped_at or datetime.now(timezone.utc).isoformat()

    seen_ids = set()
    props = []
    skipped = 0
    for raw in props_raw:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        sport = str(raw.get("sport", "mlb")).strip().lower()
        if sport not in ("mlb", "baseball"):
            continue  # not our sport - not a malformed record, just not relevant here

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
        team_norm = str(team).strip().upper()
        team_norm = DABBLE_TEAM_ABBR_OVERRIDES.get(team_norm, team_norm)
        market_norm = str(market).strip()
        pid = _prop_id(norm, team_norm, market_norm, line)
        if pid in seen_ids:
            continue  # exact-duplicate prop - collapse silently, don't double-count
        seen_ids.add(pid)

        props.append({
            "player_name": str(player_name).strip(),
            "normalized_name": norm,
            "team": team_norm,
            "market": market_norm,
            "line": line,
            "prop_id": pid,
            "scraped_at": scraped_at,
            "matchup": raw.get("matchup"),
            "event_date": str(event_date).strip(),
            "position": (str(raw["position"]).strip().upper() if raw.get("position") else None),
        })

    if skipped:
        print(f"dabble_adapter: skipped {skipped} malformed/unusable prop record(s) out of {len(props_raw)}.")
    return props


def group_props_by_player(props):
    """[prop, ...] -> {(normalized_name, team): {player_name, normalized_name,
    team, position, event_date, matchup, props: [{market, line, prop_id,
    scraped_at}, ...]}} - one record per player regardless of how many
    markets they have a live prop on. This is the actual dedup point for
    "duplicate props for the same player must not create duplicate player
    scoring": every prop for the same (normalized_name, team) key folds
    into ONE player record that gets scored exactly once.

    Pitchers are excluded here - same reasoning and same constants as
    board_loader.to_mlb_betr_entries: a starting pitcher is NEVER in the
    9-slot batting order, so "not in the lineup" is trivially/meaninglessly
    true for every pitcher on the board and would otherwise show up as a
    false CONFIRMED_NOT_STARTING_SCORE (found live 2026-09-13 testing
    against a real board - Tyler Mahle, Brandon Pfaadt and every other
    probable starter that day scored 97 for a reason that has nothing to
    do with DNP risk)."""
    players = {}
    positions_seen = {}
    pitcher_market_hit = set()
    for p in props:
        key = (p["normalized_name"], p["team"])
        if p["position"]:
            positions_seen.setdefault(key, set()).add(p["position"])
        if p["market"].strip().upper() in PITCHER_ONLY_MARKETS:
            pitcher_market_hit.add(key)
        entry = players.setdefault(key, {
            "player_name": p["player_name"],
            "normalized_name": p["normalized_name"],
            "team": p["team"],
            "position": p["position"],
            "event_date": p["event_date"],
            "matchup": p["matchup"],
            "props": [],
        })
        if p["position"] and not entry["position"]:
            entry["position"] = p["position"]
        entry["props"].append({
            "market": p["market"], "line": p["line"],
            "prop_id": p["prop_id"], "scraped_at": p["scraped_at"],
        })

    pitchers = {
        key for key in players
        if (positions_seen.get(key) or set()) & PITCHER_POSITIONS or key in pitcher_market_hit
    }
    return {key: v for key, v in players.items() if key not in pitchers}


# ---------------------------------------------------------------------------
# Step 2: shared per-slate MLB context (games/lineups/rosters/pitchers),
# resolved ONCE regardless of how many hundreds of props reference it.
# ---------------------------------------------------------------------------

def _build_mlb_context(players):
    dates = set()
    for p in players.values():
        dt = _parse_iso(p["event_date"])
        if dt:
            for delta in (-1, 0, 1):
                dates.add((dt + timedelta(days=delta)).strftime("%Y-%m-%d"))

    games = []
    for d in dates:
        try:
            games.extend(get_games(d))
        except Exception as e:
            print(f"dabble_adapter: get_games({d}) failed: {e}")
    games = _dedupe_games(games)
    lineup_index = _build_lineup_index(games)
    team_abbrevs = get_team_abbreviations()

    return {
        "games": games,
        "lineup_index": lineup_index,
        "team_abbrevs": team_abbrevs,
        "roster_cache": {},   # team_id -> {normalized_name: player_id}
        "pitcher_cache": {},  # game_pk -> get_game_start_data() result
    }


def _resolve_player_context(player, ctx):
    """Best-effort resolution of everything downstream scoring needs for
    one player: game_pk/opponent/game_start (via matchup + schedule),
    projected_start_status (via the SAME lineup-posted/lineup-started index
    stale_lines.py's hard detector already relies on), opposing pitcher
    handedness (via a live-feed probable-pitcher lookup, cached per
    game_pk), and their MLB player_id (via active-roster name matching,
    same pattern as stale_lines.run_poll's roster_cache) so start_history
    lookups can run. Every field defaults to None/"unknown" on failure
    rather than raising - one player's bad data (unmapped team, no
    schedule match, name that doesn't hit any active roster) must never
    take down scoring for the other few hundred players on the board."""
    team_abbr = player["team"]
    team_id = ctx["team_abbrevs"].get(team_abbr)

    opp_abbr = None
    matchup = player.get("matchup")
    if matchup:
        parts = _split_matchup(str(matchup))
        if parts:
            away_abbr, home_abbr = parts
            if team_abbr == away_abbr:
                opp_abbr = home_abbr
            elif team_abbr == home_abbr:
                opp_abbr = away_abbr

    event_dt = _parse_iso(player["event_date"])
    game, is_home = (None, None)
    if team_id is not None and event_dt is not None:
        game, is_home = _find_game_for_team(ctx["games"], team_id, event_dt)

    game_pk = game["gamePk"] if game else None
    game_start = game["gameDate"] if game else player["event_date"]

    start_status = "unknown"
    if game_pk is not None and game_pk in ctx["lineup_index"]:
        idx = ctx["lineup_index"][game_pk]
        started_set = idx["home_started"] if is_home else idx["away_started"]
        posted = idx["home_posted"] if is_home else idx["away_posted"]
        if player["normalized_name"] in started_set:
            start_status = "confirmed_starting"
        elif posted:
            start_status = "confirmed_not_starting"
        else:
            start_status = "not_yet_posted"

    opp_pitcher_hand = None
    if game_pk is not None:
        if game_pk not in ctx["pitcher_cache"]:
            try:
                ctx["pitcher_cache"][game_pk] = get_game_start_data(game_pk)
            except Exception:
                ctx["pitcher_cache"][game_pk] = None
        game_data = ctx["pitcher_cache"][game_pk]
        if game_data:
            side = "home" if is_home else "away"
            opp_pitcher_hand = game_data[side]["opp_pitcher_hand"]

    player_id = None
    if team_id is not None:
        if team_id not in ctx["roster_cache"]:
            try:
                ctx["roster_cache"][team_id] = {
                    normalize_name(name): pid for pid, name in get_active_roster(team_id).items()
                }
            except Exception:
                ctx["roster_cache"][team_id] = {}
        player_id = ctx["roster_cache"][team_id].get(player["normalized_name"])

    return {
        "opponent": opp_abbr,
        "game_id": game_pk,
        "game_start": game_start,
        "projected_start_status": start_status,
        "opposing_pitcher_hand": opp_pitcher_hand,
        "player_id": player_id,
    }


# ---------------------------------------------------------------------------
# Step 3: enrich + score every player
# ---------------------------------------------------------------------------

def enrich_and_score_player(player, ctx, history, news_risk):
    """One player record (from group_props_by_player) -> a fully enriched,
    scored candidate dict. Never raises - a missing enrichment source
    (unresolved game, no start history, no roster match) degrades that one
    signal gracefully rather than blocking the player from being scored at
    all - see dnp_score.score_candidate's own per-signal guards."""
    rctx = _resolve_player_context(player, ctx)
    pid = rctx["player_id"]

    is_early_game = dnp_score._is_early_game(rctx["game_start"])
    score, reasons = dnp_score.score_candidate(
        player_id=pid,
        name=player["player_name"],
        position=player.get("position"),
        start_status=rctx["projected_start_status"],
        opp_pitcher_hand=rctx["opposing_pitcher_hand"],
        is_early_game=is_early_game,
        history=history,
        news_risk=news_risk,
    )

    wins, losses, n = start_history.start_rate(history, pid, n_games=10) if pid else (0, 0, 0)
    historical_start_rate = round(100 * wins / n, 1) if n else None
    consecutive = start_history.consecutive_starts(history, pid) if pid else 0

    platoon_start_rate = None
    if pid and rctx["opposing_pitcher_hand"]:
        hw, hl, hn = start_history.start_rate_vs_hand(history, pid, rctx["opposing_pitcher_hand"], n_games=15)
        if hn:
            platoon_start_rate = round(100 * hw / hn, 1)

    news_hit = news_risk.get(player["normalized_name"])
    confidence = dnp_score.compute_confidence(
        start_status=rctx["projected_start_status"],
        recent_n=n,
        opp_hand_known=rctx["opposing_pitcher_hand"] is not None,
        game_resolved=rctx["game_id"] is not None,
    )

    return {
        "player_name": player["player_name"],
        "normalized_name": player["normalized_name"],
        "team": player["team"],
        "opponent": rctx["opponent"],
        "game_id": rctx["game_id"],
        "game_start": rctx["game_start"],
        "position": player.get("position"),
        "player_id": pid,
        "props": player["props"],
        # --- enrichment fields (requested explicitly - kept visible on the
        # candidate, not just folded opaquely into the score) ---
        "projected_start_status": rctx["projected_start_status"],
        "historical_start_rate": historical_start_rate,
        "recent_start_rate_n": n,
        "opposing_pitcher_hand": rctx["opposing_pitcher_hand"],
        "platoon_start_rate": platoon_start_rate,
        "consecutive_starts": consecutive,
        "injury_rest_note": news_hit[1] if news_hit else None,
        # --- score ---
        "score": score,
        "tier": dnp_score.tier_label(score),
        "confidence": confidence,
        "reasons": reasons,
    }


def score_dabble_board(source=DEFAULT_BOARD_PATH, persist=True, snapshot_date=None):
    props = load_dabble_props(source)
    players = group_props_by_player(props)
    if not players:
        print("dabble_adapter: no MLB players found on this board.")
        return []

    ctx = _build_mlb_context(players)
    history = start_history._load(start_history.HISTORY_PATH, {})
    news_risk = dnp_score._load_watchlist_risk_map()

    candidates = [enrich_and_score_player(p, ctx, history, news_risk) for p in players.values()]
    candidates.sort(key=lambda c: c["score"], reverse=True)

    if persist:
        record_live_snapshot(snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"), candidates)

    return candidates


# ---------------------------------------------------------------------------
# Step 4: timestamped live snapshots - "how long before lineup confirmation
# did a player first cross 60/70/80/90"
# ---------------------------------------------------------------------------

def _snapshot_key(candidate):
    """Prefer the resolved MLB player_id; fall back to normalized_name+team
    for a player we couldn't match to an active roster (still worth
    tracking - an unresolved roster match is itself useful information,
    not a reason to drop the player from history)."""
    if candidate.get("player_id") is not None:
        return f"pid:{candidate['player_id']}"
    return f"name:{candidate['normalized_name']}:{candidate['team']}"


def record_live_snapshot(date_str, candidates):
    """Append this run's score to every candidate's running score_history
    and stamp first_crossed[threshold] the first time each of
    THRESHOLDS_TO_TRACK is reached - data/dnp_live_snapshots/{date}.json.
    Safe to call many times a day (e.g. once per poll cycle); each call
    only ever appends/fills in, never overwrites prior history."""
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
    for c in candidates:
        key = _snapshot_key(c)
        entry = data.setdefault(key, {
            "name": c["player_name"], "team": c["team"], "player_id": c.get("player_id"),
            "game_id": c.get("game_id"),
            "score_history": [], "first_crossed": {str(t): None for t in THRESHOLDS_TO_TRACK},
        })
        entry["name"] = c["player_name"]
        entry["player_id"] = c.get("player_id")
        if c.get("game_id") is not None:
            entry["game_id"] = c["game_id"]  # keep latest resolved game_id (a poll early in the day may not have resolved it yet)
        entry["score_history"].append({"ts": now_iso, "score": c["score"],
                                        "start_status": c["projected_start_status"]})
        for t in THRESHOLDS_TO_TRACK:
            if c["score"] >= t and entry["first_crossed"][str(t)] is None:
                entry["first_crossed"][str(t)] = now_iso

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def grade_live_snapshot(date_str):
    """Grade a date's live snapshot file against real outcomes, same
    approach as dnp_score.grade_dnp_scores (reuses get_game_start_data,
    the same source start_history.py backfills from) - answers "how long
    before lineup confirmation did this player first cross each
    threshold, and were we right." Returns the list of graded entries
    (not persisted separately - the live snapshot file itself already has
    everything; this just adds the actual outcome for inspection/analysis)."""
    path = os.path.join(LIVE_SNAPSHOTS_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        print(f"dabble_adapter: no live snapshot for {date_str}.")
        return []

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    game_cache = {}
    graded = []
    for key, entry in data.items():
        game_id = entry.get("game_id")
        pid = entry.get("player_id")
        if game_id is None or pid is None:
            continue
        if game_id not in game_cache:
            game_cache[game_id] = get_game_start_data(game_id)
        game_data = game_cache[game_id]
        if not game_data:
            continue
        actual_started = None
        for side in ("home", "away"):
            for p in game_data[side]["players"]:
                if p.get("player_id") == pid:
                    actual_started = p["started"]
                    break
            if actual_started is not None:
                break
        if actual_started is None:
            continue
        graded.append({
            "key": key, "name": entry["name"], "actual_started": actual_started,
            "first_crossed": entry["first_crossed"], "final_score": entry["score_history"][-1]["score"],
        })
    return graded


# ---------------------------------------------------------------------------
# Live ranked output
# ---------------------------------------------------------------------------

def print_ranked(candidates, top_n=20):
    shown = candidates[:top_n]
    if not shown:
        print("No candidates.")
        return
    print(f"{'Score':<7}{'Conf':<6}{'Player':<24}{'Game':<16}{'Market/Line':<32}{'Status':<22}Top reasons")
    for c in shown:
        game = f"{c['team']}@{c['opponent']}" if c["opponent"] else c["team"]
        props_str = ", ".join(f"{p['market']} {p['line']}" for p in c["props"][:2])
        if len(c["props"]) > 2:
            props_str += f" (+{len(c['props']) - 2})"
        if len(props_str) > 31:
            props_str = props_str[:28] + "..."
        reasons = "; ".join(c["reasons"][:2])
        print(f"{c['score']:<7}{c['confidence']:<6}{c['player_name']:<24}{game:<16}{props_str:<32}"
              f"{c['projected_start_status']:<22}{reasons}")


def main():
    parser = argparse.ArgumentParser(description="Score the live Dabble MLB board for DNP risk")
    parser.add_argument("--source", default=DEFAULT_BOARD_PATH, help="path to a Dabble board JSON")
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--no-persist", action="store_true", help="don't write a live snapshot")
    args = parser.parse_args()

    candidates = score_dabble_board(source=args.source, persist=not args.no_persist)
    print(f"\nScored {len(candidates)} MLB players from {args.source}.\n")
    print_ranked(candidates, top_n=args.top)


if __name__ == "__main__":
    main()
