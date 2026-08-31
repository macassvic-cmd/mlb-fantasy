"""
Stale-line detector (Phase 2, opened 2026-08-31; corrected 2026-08-31):
flags Betr hitter lines posted for a player who is NOT in the confirmed
MLB starting lineup - a signal that Betr hasn't pulled a line yet for
someone scratched/benched/demoted after the line was set.

STRATEGY IS DID-NOT-START (DNS), NOT STAT-BASED. The bet is that the
flagged player does not START - full stop. It does NOT matter what stat
market the line is posted on, what the line's value is, or what the
player does if they later sub in. A benched player who pinch-hits and
scores heavily is STILL A WIN, because the bet resolves purely on
starting-lineup membership, not performance. This is a correction from
this module's first version, which gated new flags on a Betr
FANTASY_POINTS line >= 4.0 and framed the backtest around "did the
flagged player score near zero" - both wrong for this strategy. See git
history for that version if the stat-based framing is ever needed for a
different product.

RELEVANCE: ANY player with at least one posted Betr hitter market
(HITS, TOTAL_BASES, SINGLES, RUNS, RBI, WALKS, STRIKEOUTS,
FANTASY_POINTS - all of them) is flag-eligible. No line-value filter -
line size is irrelevant to a DNS bet. The only gates left are:
  1. First pitch >= MIN_MINUTES_TO_FIRST_PITCH out (no window to act
     otherwise - see run_poll).
  2. Not already in the confirmed starting lineup (obviously - that's
     the thing being bet against).

Corrected Phase 1 backtest (42 settled historical days, ALL markets, no
value filter - see session notes for the full table): ~9-10 flags/day.
Of players flagged this way, checked directly against the true lineup
card for that date, the false-positive rate (flagged as not-starting but
genuinely DID start) was ~0% - the schedule-hydration lineup card is
authoritative once posted, so a flag against a POSTED lineup is correct
by construction; the only way to be wrong is if the flag fires before
the real lineup is posted (an early or reversed decision) or if the
lineup source itself has a data gap. That reframes the risk correctly:
it's not "the sub still performed" (irrelevant to DNS) - it's purely
"was our lineup source wrong or premature."

FALSE-POSITIVE (loss) PROTECTION - i.e., "our lineup source was wrong":
  1. "lineup_unavailable" category: if the team's side of the schedule
     hydration has ZERO players listed, the lineup genuinely hasn't been
     posted yet by MLB - NOT a confirmed absence. Tracked separately,
     re-evaluated every poll, never treated as a win until a real
     lineup card is checked.
  2. Second-source cross-check at flag creation: when the team's lineup
     IS posted and the player is absent from it, cross-checks the
     live-feed boxscore's battingOrder (mlb_api.get_live_feed_batting_
     orders) - a genuinely separate MLB code path. If the player
     appears there as a starter, the flag is suppressed before it's
     even created.
  3. Ongoing grading: every unresolved flag is re-checked against the
     CURRENT confirmed lineup on every poll (via a lineup index rebuilt
     from that poll's own schedule fetch, independent of whether the
     player still has a live Betr line - see the note in run_poll about
     why this can't be nested inside the per-Betr-entry loop). If the
     player shows up in the confirmed lineup at any point before or at
     game time, the flag is graded a LOSS ("started_after_all") -
     otherwise, once the lineup is posted and the window has closed
     (game has started, or 30 post-first-seen minutes have elapsed with
     the player still absent), it's graded a WIN
     ("confirmed_not_started"). If the lineup for that side is STILL
     never posted even by game time (should be extremely rare - see
     empirical check in session notes: 0 such incidents in a 10-day
     sample), the flag resolves with grade=None
     ("unresolved_lineup_never_posted") rather than being silently
     counted as a win.

MEASUREMENT (this phase is LOG-ONLY - no notifications yet, see Phase 3):
independent of DNS grading, every active flag is also checked at 5/15/30
minutes past first-seen for whether Betr still has ANY line posted for
that player. This answers a separate question from grading: does the
stale line get pulled within our alerting window, or does it linger long
enough to be worth acting on?
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


def _build_lineup_index(games):
    """{game_pk: {"home_posted":, "away_posted":, "home_started": {names},
    "away_started": {names}, "game_time_utc":}} - built once per poll from
    that poll's own schedule fetch, and consulted BOTH when deciding
    whether to create a new flag AND, separately, when grading every
    already-tracked flag. Keeping this as a single shared lookup (rather
    than re-deriving started/posted status ad hoc in two different places)
    is what makes it possible to grade a flag correctly even after its
    player's Betr line has vanished entirely - see run_poll's grading
    pass, which does NOT depend on the player still appearing in the
    current Betr fetch."""
    index = {}
    for g in games:
        ld = g.get("lineups", {})
        home_players = ld.get("homePlayers", [])
        away_players = ld.get("awayPlayers", [])
        index[g["gamePk"]] = {
            "home_posted": len(home_players) > 0,
            "away_posted": len(away_players) > 0,
            "home_started": {normalize_name(p["fullName"]) for p in home_players},
            "away_started": {normalize_name(p["fullName"]) for p in away_players},
            "game_time_utc": g["gameDate"],
        }
    return index


def run_poll():
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    games = _dedupe_games(get_games(today_str) + get_games(tomorrow_str))
    lineup_index = _build_lineup_index(games)
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
        "too_close_to_first_pitch": 0,
        "suppressed_by_second_source": 0,
        "new_flags_lineup_unavailable": 0,
        "new_flags_not_in_lineup": 0,
        "already_flagged_seen_again": 0,
    }
    checkpoint_events = []
    grading_events = []
    roster_cache = {}  # team_id -> {normalized_name: mlb_player_id}, this poll only

    # --- Pass 1: create new flags from this poll's Betr entries ---------
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
        idx = lineup_index[game_pk]
        game_dt = _parse_iso(idx["game_time_utc"])
        minutes_to_first_pitch = (game_dt - now).total_seconds() / 60
        started_names = idx["home_started"] if is_home else idx["away_started"]
        lineup_posted = idx["home_posted"] if is_home else idx["away_posted"]

        flag_id = f"{game_pk}:{entry['normalized_name']}"
        if flag_id in state["flags"]:
            counters["already_flagged_seen_again"] += 1
            continue

        if entry["normalized_name"] in started_names:
            counters["already_started"] += 1
            continue  # confirmed starting - never eligible for a flag, no DNS bet to make

        if minutes_to_first_pitch < MIN_MINUTES_TO_FIRST_PITCH:
            counters["too_close_to_first_pitch"] += 1
            continue  # no window to act

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

        flag = {
            "flag_id": flag_id,
            "name": entry["name"],
            "normalized_name": entry["normalized_name"],
            "team": team_abbr,
            "game_pk": game_pk,
            "is_home": is_home,
            "game_time_utc": idx["game_time_utc"],
            "category": category,
            "all_markets": entry["markets"],
            "first_seen_utc": now.isoformat(),
            "minutes_to_first_pitch_at_flag": round(minutes_to_first_pitch, 1),
            "checks": [],
            "resolved": False,
            "grade": None,
            "resolution": None,
        }
        state["flags"][flag_id] = flag
        _log_event({
            "ts": now.isoformat(), "type": "new_flag", "flag_id": flag_id,
            "name": entry["name"], "team": team_abbr, "category": category,
            "markets": entry["markets"], "minutes_to_first_pitch": round(minutes_to_first_pitch, 1),
            "game_time_utc": idx["game_time_utc"],
        })

    # --- Pass 2: grade + measure every unresolved flag -------------------
    # A SEPARATE pass over every flag in state, not folded into pass 1.
    # Grading needs to keep checking a flag's TRUE lineup status even
    # after its player's Betr line vanishes entirely from betr_entries
    # (which is exactly what tends to happen once a scratched player is
    # confirmed out, OR once a player starts and the prop becomes moot) -
    # if grading only ran inside the pass-1 loop, a flag would freeze the
    # instant its player stopped appearing in the current Betr fetch,
    # silently missing exactly the "started after all" losses this
    # module exists to catch.
    for flag_id, flag in state["flags"].items():
        if flag.get("resolved"):
            continue

        idx = lineup_index.get(flag["game_pk"])
        if idx is not None:
            side_started = idx["home_started"] if flag["is_home"] else idx["away_started"]
            side_posted = idx["home_posted"] if flag["is_home"] else idx["away_posted"]
            game_dt = _parse_iso(idx["game_time_utc"])

            if flag["normalized_name"] in side_started:
                flag["resolved"] = True
                flag["grade"] = "loss"
                flag["resolution"] = "started_after_all"
                grading_events.append({"flag_id": flag_id, "name": flag["name"], "grade": "loss"})
                _log_event({"ts": now.isoformat(), "type": "graded", "flag_id": flag_id, "name": flag["name"], "grade": "loss", "resolution": "started_after_all"})
                continue

            if flag["category"] == "lineup_unavailable" and side_posted:
                flag["category"] = "not_in_lineup"

            if game_dt <= now:
                if side_posted:
                    flag["resolved"] = True
                    flag["grade"] = "win"
                    flag["resolution"] = "confirmed_not_started"
                else:
                    flag["resolved"] = True
                    flag["grade"] = None
                    flag["resolution"] = "unresolved_lineup_never_posted"
                grading_events.append({"flag_id": flag_id, "name": flag["name"], "grade": flag["grade"]})
                _log_event({"ts": now.isoformat(), "type": "graded", "flag_id": flag_id, "name": flag["name"], "grade": flag["grade"], "resolution": flag["resolution"]})
                continue
        # else: this flag's game fell outside today+tomorrow's fetched
        # schedule window (shouldn't normally happen given the 30-min
        # creation gate, but possible for a very delayed game) - can't
        # grade this poll, fall through to the measurement-only check
        # below without resolving.

        first_seen = _parse_iso(flag["first_seen_utc"])
        elapsed_min = (now - first_seen).total_seconds() / 60
        done_thresholds = {c["elapsed_min"] for c in flag["checks"]}
        still_live_entry = betr_by_name.get(flag["normalized_name"])
        still_has_any_market = flag["normalized_name"] in live_names_now
        for threshold in CHECK_INTERVALS_MIN:
            if threshold in done_thresholds or elapsed_min < threshold:
                continue
            check = {
                "elapsed_min": threshold,
                "checked_at_utc": now.isoformat(),
                "still_has_any_market": still_has_any_market,
                "still_posted_markets": (still_live_entry["markets"] if still_live_entry else None),
            }
            flag["checks"].append(check)
            checkpoint_events.append({**check, "flag_id": flag_id, "name": flag["name"]})
            _log_event({"ts": now.isoformat(), "type": "checkpoint", "flag_id": flag_id, "name": flag["name"], **check})

    save_state(state)

    active_flags = [f for f in state["flags"].values() if not f.get("resolved")]
    wins = sum(1 for f in state["flags"].values() if f.get("grade") == "win")
    losses = sum(1 for f in state["flags"].values() if f.get("grade") == "loss")
    unresolved_grade = sum(1 for f in state["flags"].values() if f.get("resolved") and f.get("grade") is None)
    summary = {
        "polled_at_utc": now.isoformat(),
        "counters": counters,
        "checkpoint_events_this_poll": checkpoint_events,
        "grading_events_this_poll": grading_events,
        "active_unresolved_flags": len(active_flags),
        "total_tracked_flags": len(state["flags"]),
        "record": {"wins": wins, "losses": losses, "unresolved_lineup_never_posted": unresolved_grade},
    }
    return summary


def main():
    summary = run_poll()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
