"""
Stale-line detector (Phase 2, opened 2026-08-31): flags Betr hitter lines
posted for a player who is NOT in tonight's confirmed MLB starting lineup -
a signal that Betr hasn't pulled a line yet for someone scratched/benched/
demoted after the line was set.

Phase 1 backtest (42 settled days of banked Betr snapshots) found:
  - ~6 flags/day on Betr's full hitter board, not the ~2/week originally
    assumed - this shaped the relevance filter below.
  - 87.0% of flagged players scored exactly 0 UD fantasy points that day,
    95.3% scored <=5. The "benched = near-automatic under" assumption
    holds strongly but is NOT risk-free.
  - One apparent miss (Fernando Tatis Jr., 2026-08-12, 16 pts) turned out
    on inspection to be a genuine LATE SUBSTITUTE appearance (2 BB, 1 R,
    2 SB - both the schedule hydration and the live-feed boxscore agreed
    he wasn't a starter; battingOrder "901" = a mid-game sub into the
    9-slot). Not a lineup-data gap - no pre-game signal could have caught
    it. The protections below (lineup-availability check, second-source
    cross-check) are still worth having for genuine data-quality gaps,
    but they would NOT have prevented this specific case. This residual
    risk is inherent non-starter variance, already reflected in the
    87-95% figures above.

RELEVANCE FILTER: a NEW flag requires the player's Betr FANTASY_POINTS
line (Betr's own composite-score market, the only one on the same scale
as UD/PP's ~5-10pt lines) to be >= RELEVANT_LINE_MIN, AND first pitch to
be >= MIN_MINUTES_TO_FIRST_PITCH out. IMPORTANT SCALE NOTE: every other
Betr hitter market (HITS, SINGLES, TOTAL_BASES, RUNS, HITS_RUNS_RBI, ...)
is a single-stat prop line that tops out around 2.5 in practice - it can
never cross a 4.0 threshold. Applying "line >= 4.0" literally therefore
restricts NEW flags almost entirely to players who have a posted
FANTASY_POINTS line specifically (~2.4% of raw flags in the Phase 1
data) - not a blanket filter across all markets. Every flag still
records ALL of that player's posted markets for visibility even though
only FANTASY_POINTS gates eligibility.

FALSE-NEGATIVE PROTECTION:
  1. "lineup_unavailable" category: if the team's side of the schedule
     hydration (get_lineups' underlying schedule?hydrate=lineups) has
     ZERO players listed, that means the lineup genuinely hasn't been
     posted yet by MLB - NOT that this specific player was scratched.
     Flagged separately, re-evaluated every poll, not treated as a
     confirmed absence.
  2. Second-source cross-check: when the team's lineup IS posted and the
     player is absent from it, cross-checks the live-feed boxscore's
     battingOrder (mlb_api.get_live_feed_batting_orders) - a genuinely
     separate MLB code path, confirmed empirically to populate pre-game.
     If the player appears there as a starter, the flag is suppressed
     (source 1 disagreed with source 2 - trust source 2 rather than
     alert on a stale read of source 1).

MEASUREMENT (this phase is LOG-ONLY - no notifications yet, see Phase 3):
every active (unresolved) flag is re-checked on every poll; at 5/15/30
minutes past first-seen, records whether Betr still has ANY line posted
for that player and whether the specific FANTASY_POINTS line survives.
Answers: does the stale line get pulled within our alerting window, or
does it linger long enough to be worth acting on?
"""

import json
import os
from datetime import datetime, timedelta, timezone

from scrapers.betr import fetch_betr_hitter_lines_with_context, normalize_name
from scrapers.mlb_api import (
    get_games,
    get_team_abbreviations,
    get_active_roster,
    get_live_feed_batting_orders,
)

STATE_DIR = os.path.join("data", "stale_lines")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
EVENTS_LOG_PATH = os.path.join(STATE_DIR, "events.jsonl")

RELEVANT_LINE_MIN = 4.0
MIN_MINUTES_TO_FIRST_PITCH = 30
CHECK_INTERVALS_MIN = [5, 15, 30]

# Betr's team.name matches MLB's own /api/v1/teams abbreviation for 28 of
# 30 teams (confirmed empirically 2026-08-31) - these two use a different
# convention. Logged as "no_team_match" in counters if any OTHER mismatch
# ever surfaces, so a future Betr naming change won't silently vanish.
BETR_TEAM_ABBR_OVERRIDES = {
    "CHW": "CWS",  # Chicago White Sox - MLB uses CWS
    "ARI": "AZ",   # Arizona Diamondbacks - MLB uses AZ
}


def _parse_iso(ts):
    """Parse an MLB/Betr ISO timestamp (may end in bare 'Z') into an aware
    UTC datetime. Betr's own format omits seconds ('...T22:05Z'); MLB's
    includes them ('...T22:05:00Z') - both handled the same way."""
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _log_event(event):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(EVENTS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"flags": {}}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _dedupe_games(games):
    seen = set()
    out = []
    for g in games:
        pk = g["gamePk"]
        if pk in seen:
            continue
        seen.add(pk)
        out.append(g)
    return out


def _find_game_for_team(games, team_id, near_utc):
    """The game (today or tomorrow's schedule) that team_id is playing in,
    closest in time to near_utc (Betr's own event_date_utc) - handles the
    rare doubleheader case where a team appears in two games same day."""
    candidates = []
    for g in games:
        home_id = g["teams"]["home"]["team"]["id"]
        away_id = g["teams"]["away"]["team"]["id"]
        if team_id in (home_id, away_id):
            candidates.append((g, home_id == team_id))
    if not candidates:
        return None, None
    if len(candidates) == 1:
        return candidates[0]
    return min(candidates, key=lambda c: abs((_parse_iso(c[0]["gameDate"]) - near_utc).total_seconds()))


def run_poll():
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    games = _dedupe_games(get_games(today_str) + get_games(tomorrow_str))
    team_abbrevs = get_team_abbreviations()
    betr_entries = fetch_betr_hitter_lines_with_context()
    live_names_now = {e["normalized_name"] for e in betr_entries}
    betr_by_name = {e["normalized_name"]: e for e in betr_entries}

    state = load_state()
    state.setdefault("flags", {})

    counters = {
        "betr_hitter_entries": len(betr_entries),
        "no_team_match": 0,
        "already_started": 0,
        "below_relevance_line": 0,
        "too_close_to_first_pitch": 0,
        "suppressed_by_second_source": 0,
        "new_flags_lineup_unavailable": 0,
        "new_flags_not_in_lineup": 0,
        "already_flagged_seen_again": 0,
    }
    checkpoint_events = []
    roster_cache = {}  # team_id -> {normalized_name: mlb_player_id}, this poll only

    for entry in betr_entries:
        team_abbr = entry["team"]
        team_id = team_abbrevs.get(team_abbr) or team_abbrevs.get(BETR_TEAM_ABBR_OVERRIDES.get(team_abbr))
        event_dt = _parse_iso(entry["event_date_utc"])
        if team_id is None or event_dt is None:
            counters["no_team_match"] += 1
            continue

        game, is_home = _find_game_for_team(games, team_id, event_dt)
        if game is None:
            counters["no_team_match"] += 1
            continue

        game_pk = game["gamePk"]
        game_dt = _parse_iso(game["gameDate"])
        minutes_to_first_pitch = (game_dt - now).total_seconds() / 60

        ld = game.get("lineups", {})
        side_players = ld.get("homePlayers" if is_home else "awayPlayers", [])
        lineup_posted = len(side_players) > 0
        started_names = {normalize_name(p["fullName"]) for p in side_players}

        flag_id = f"{game_pk}:{entry['normalized_name']}"
        existing = state["flags"].get(flag_id)

        if entry["normalized_name"] in started_names:
            counters["already_started"] += 1
            if existing and not existing.get("resolved"):
                existing["resolved"] = True
                existing["resolution"] = "started_after_all"
                _log_event({"ts": now.isoformat(), "type": "resolved_started_after_all", "flag_id": flag_id, "name": entry["name"]})
            continue

        if existing is None:
            fp_line = entry["markets"].get("FANTASY_POINTS")
            relevant = fp_line is not None and fp_line >= RELEVANT_LINE_MIN
            if not relevant:
                counters["below_relevance_line"] += 1
                continue
            if minutes_to_first_pitch < MIN_MINUTES_TO_FIRST_PITCH:
                counters["too_close_to_first_pitch"] += 1
                continue

            if not lineup_posted:
                category = "lineup_unavailable"
                counters["new_flags_lineup_unavailable"] += 1
            else:
                if team_id not in roster_cache:
                    roster_cache[team_id] = {normalize_name(name): pid for pid, name in get_active_roster(team_id).items()}
                mlb_pid = roster_cache[team_id].get(entry["normalized_name"])
                if mlb_pid is not None:
                    orders = get_live_feed_batting_orders(game_pk)
                    side_order = orders["home" if is_home else "away"]
                    if side_order and mlb_pid in side_order:
                        counters["suppressed_by_second_source"] += 1
                        _log_event({"ts": now.isoformat(), "type": "suppressed_second_source", "name": entry["name"], "team": team_abbr, "game_pk": game_pk})
                        continue
                category = "not_in_lineup"
                counters["new_flags_not_in_lineup"] += 1

            existing = {
                "flag_id": flag_id,
                "name": entry["name"],
                "normalized_name": entry["normalized_name"],
                "team": team_abbr,
                "game_pk": game_pk,
                "game_time_utc": game["gameDate"],
                "category": category,
                "fantasy_points_line": fp_line,
                "all_markets": entry["markets"],
                "first_seen_utc": now.isoformat(),
                "minutes_to_first_pitch_at_flag": round(minutes_to_first_pitch, 1),
                "checks": [],
                "resolved": False,
            }
            state["flags"][flag_id] = existing
            _log_event({
                "ts": now.isoformat(), "type": "new_flag", "flag_id": flag_id,
                "name": entry["name"], "team": team_abbr, "category": category,
                "fantasy_points_line": fp_line, "markets": entry["markets"],
                "minutes_to_first_pitch": round(minutes_to_first_pitch, 1),
                "game_time_utc": game["gameDate"],
            })
        else:
            counters["already_flagged_seen_again"] += 1
            # A previously lineup_unavailable flag may now have a posted
            # lineup - re-derive category so it stops reading as
            # unresolved once MLB catches up.
            if existing.get("category") == "lineup_unavailable" and lineup_posted:
                existing["category"] = "not_in_lineup"

    # Measurement pass - deliberately a SEPARATE pass over every flag
    # currently in state, not folded into the per-Betr-entry loop above.
    # If it were folded in, a flag whose player's line gets pulled
    # entirely (vanishes from betr_entries) would simply never be
    # revisited by that loop again - silently freezing its measurement
    # forever instead of recording "line disappeared," which is exactly
    # the outcome this phase exists to measure. Decoupling means every
    # unresolved flag gets checked every poll regardless of whether its
    # player still appears in the current Betr fetch.
    for flag_id, flag in state["flags"].items():
        if flag.get("resolved"):
            continue
        first_seen = _parse_iso(flag["first_seen_utc"])
        elapsed_min = (now - first_seen).total_seconds() / 60
        done_thresholds = {c["elapsed_min"] for c in flag["checks"]}
        still_live_entry = betr_by_name.get(flag["normalized_name"])
        still_has_any_market = flag["normalized_name"] in live_names_now
        still_has_fp_line = bool(still_live_entry and still_live_entry["markets"].get("FANTASY_POINTS") is not None)
        for threshold in CHECK_INTERVALS_MIN:
            if threshold in done_thresholds or elapsed_min < threshold:
                continue
            check = {
                "elapsed_min": threshold,
                "checked_at_utc": now.isoformat(),
                "still_has_any_market": still_has_any_market,
                "still_has_fantasy_points_line": still_has_fp_line,
                "current_fantasy_points_line": (still_live_entry["markets"].get("FANTASY_POINTS") if still_live_entry else None),
            }
            flag["checks"].append(check)
            checkpoint_events.append({**check, "flag_id": flag_id, "name": flag["name"]})
            _log_event({"ts": now.isoformat(), "type": "checkpoint", "flag_id": flag_id, "name": flag["name"], **check})
        game_dt = _parse_iso(flag["game_time_utc"])
        if game_dt <= now or len(flag["checks"]) >= len(CHECK_INTERVALS_MIN):
            flag["resolved"] = True
            if "resolution" not in flag:
                flag["resolution"] = "measurement_complete" if len(flag["checks"]) >= len(CHECK_INTERVALS_MIN) else "game_started"

    save_state(state)

    active_flags = [f for f in state["flags"].values() if not f.get("resolved")]
    summary = {
        "polled_at_utc": now.isoformat(),
        "counters": counters,
        "checkpoint_events_this_poll": checkpoint_events,
        "active_unresolved_flags": len(active_flags),
        "total_tracked_flags": len(state["flags"]),
    }
    return summary


def main():
    summary = run_poll()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
