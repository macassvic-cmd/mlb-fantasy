"""
Soccer DNS (did-not-play) detector - Phase 0/1, opened 2026-09-02.

Same betting logic as stale_lines.py's MLB detector (see that module's
docstring for the full framing) but a fundamentally different risk
profile: MLB's edge is a RACE (a scratched player's line usually gets
pulled within minutes of the lineup posting - see stale_lines.py's
checkpoint data, 93% gone inside 5 minutes). Soccer's edge here is
STALE INVENTORY - a club's own official injury list is public days
before a match, no lineup-reveal race involved, so this runs ONCE A DAY
rather than polling.

DETECTION: for every player with a currently-open Betr line across the
domestic leagues Betr covers (EPL, LaLiga, Ligue 1, Bundesliga, Serie A,
MLS - see scrapers/transfermarkt.py's TEAM_SEARCH_ALIASES for the club
list), cross-reference their club's Transfermarkt injury/suspension list.
CRITICAL gate (see module-level FIXTURE_DATE note): a listed injury only
counts if the player is confirmed out THROUGH the specific fixture the
Betr line is for - an expected return 3 days before a match 5 days out
means they'll likely play, not sit. Only two cases flag:
  1. No expected-return date at all (indefinite absence), or
  2. Expected return date is AFTER the fixture date.
A player with a set return date on/before the fixture date is NOT
flagged - the data says they should be back in time.

GRADING (mirrors stale_lines.py's win/loss framing): once a flagged
fixture has kicked off and had time to finish, scrapers/espn_soccer.py's
match-squad lookup checks whether the flagged player appeared in EITHER
team's matchday squad at all (starter or unused substitute - ESPN's
roster listing, not just the starting XI). Absent from both squads
entirely -> WIN ("confirmed_did_not_play"). Present at all -> LOSS
("appeared_in_squad") - presence contradicts the "out" premise, same
"the bet resolves on membership, not performance" philosophy stale_lines
uses for MLB. If ESPN's match data still isn't available well after the
match should have finished, resolves as grade=None
("unresolved_no_espn_data") rather than being silently dropped.

DISCORD: one message per flag, posted at detection with the injury
reason/since/expected-return dates included (so a human can sanity-check
before acting - there's no race here to hide that latency behind), then
PATCHed once with the final grade once graded.
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

from scrapers.betr import normalize_name as betr_normalize_name
from scrapers import espn_soccer
from scrapers.transfermarkt import get_team_injuries_cached

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BETR_GRAPHQL_URL = "https://api.fantasy.betr.app/graphql"
BETR_HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

# The domestic leagues Betr covers with real player-prop markets and that
# scrapers/espn_soccer.py has a confirmed ESPN league-slug mapping for.
# UCL had 0 upcoming events on Betr as of 2026-09-02 (kept out - nothing
# to scan); FWC/WCC (international/national-team fixtures) are
# deliberately excluded - Transfermarkt's injury pages are organized by
# CLUB, not country, so there's no equivalent "team's injury list" to
# check for a national-team fixture without a much larger player-to-
# current-club lookup this phase doesn't build.
LEAGUES = ["EPL", "MLS", "LLG", "L1F", "BUN", "SEA"]

STATE_DIR = os.path.join("data", "soccer_dns")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
EVENTS_LOG_PATH = os.path.join(STATE_DIR, "events.jsonl")

DISCORD_WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"

# A completed match's roster/box score is reliably posted by ESPN within
# a few hours - graded no sooner than this many hours after kickoff, to
# avoid a premature "unresolved" read on a match still in progress.
GRADE_AFTER_HOURS = 4

UPCOMING_EVENTS_QUERY = """query UpcomingEventsInfo($league: League!) {
  getUpcomingEventsV2(league: $league) { ... on EventV2 { id date status } }
}"""

EVENT_PLAYERS_QUERY = """query EventInfoWithPlayers($id: String!) {
  getEventByIdV2(id: $id) {
    id date
    ... on TeamVersusEvent {
      teams {
        id name fullName
        players {
          id firstName lastName position
          projections { marketStatus key value }
        }
      }
    }
  }
}"""


def _betr_gql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = requests.post(BETR_GRAPHQL_URL, data=body, headers=BETR_HEADERS, timeout=20)
    req.raise_for_status()
    payload = req.json()
    if payload.get("errors"):
        raise RuntimeError(f"Betr GraphQL error: {payload['errors']}")
    return payload["data"]


def _parse_betr_date(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _discord_webhook_url():
    return os.environ.get(DISCORD_WEBHOOK_ENV_VAR)


def _log_event(event):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(EVENTS_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"flags": {}}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def _flag_embed(flag):
    fields = [
        {"name": "Team", "value": flag["team"], "inline": True},
        {"name": "League", "value": flag["league"], "inline": True},
        {"name": "Fixture date", "value": flag["event_date"][:10], "inline": True},
        {"name": "Injury/suspension", "value": flag["reason"], "inline": False},
        {"name": "Out since", "value": flag["since"] or "unknown", "inline": True},
        {"name": "Expected return", "value": flag["expected_return"] or "Indefinite (no date set)", "inline": True},
        {"name": "Live Betr markets", "value": ", ".join(flag["markets"]) or "(none)", "inline": False},
    ]
    if flag.get("grade"):
        fields.append({"name": "Result", "value": f"{flag['grade'].upper()} - {flag['resolution']}", "inline": False})
    return {
        "title": f"⚽ {flag['name']} — {flag['team']}",
        "description": "Live Betr line, confirmed out through this fixture per Transfermarkt.",
        "color": 0x2ECC71 if flag.get("grade") == "win" else (0xE74C3C if flag.get("grade") == "loss" else 0xE67E22),
        "fields": fields,
    }


def _discord_post(flag):
    webhook_url = _discord_webhook_url()
    if not webhook_url:
        return False
    try:
        resp = requests.post(f"{webhook_url}?wait=true", json={"embeds": [_flag_embed(flag)]}, timeout=15)
        resp.raise_for_status()
        flag["discord_message_id"] = resp.json().get("id")
        return True
    except Exception as e:
        logger.warning(f"Discord POST failed for {flag['name']}: {e}")
        return False


def _discord_edit(flag):
    webhook_url = _discord_webhook_url()
    if not webhook_url or not flag.get("discord_message_id"):
        return False
    try:
        resp = requests.patch(f"{webhook_url}/messages/{flag['discord_message_id']}",
                               json={"embeds": [_flag_embed(flag)]}, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Discord PATCH failed for {flag['name']}: {e}")
        return False


def fetch_betr_soccer_board():
    """{event_id: {"date":, "league":, "teams": {team_fullName: {norm_name:
    {"name":, "markets": set()}}}}} - every currently OPENED player market
    across all LEAGUES' upcoming events."""
    board = {}
    for league in LEAGUES:
        try:
            data = _betr_gql(UPCOMING_EVENTS_QUERY, {"league": league})
        except Exception as e:
            logger.warning(f"[{league}] upcoming-events fetch failed: {e}")
            continue
        for ev in data.get("getUpcomingEventsV2") or []:
            eid = ev["id"]
            if eid in board:
                continue
            try:
                edata = _betr_gql(EVENT_PLAYERS_QUERY, {"id": eid})
            except Exception as e:
                logger.warning(f"event {eid} fetch failed: {e}")
                continue
            event = edata.get("getEventByIdV2")
            if not event or "teams" not in event:
                continue
            teams = {}
            for team in event["teams"]:
                full_name = team.get("fullName") or team.get("name")
                bucket = teams.setdefault(full_name, {})
                for p in team.get("players", []):
                    open_markets = [pr["key"] for pr in p.get("projections", []) if pr.get("marketStatus") == "OPENED"]
                    if not open_markets:
                        continue
                    name = f"{p.get('firstName','')} {p.get('lastName','')}".strip()
                    norm = betr_normalize_name(name)
                    entry = bucket.setdefault(norm, {"name": name, "markets": set()})
                    entry["markets"].update(open_markets)
            board[eid] = {"date": event.get("date"), "league": league, "teams": teams}
    return board


def _confirmed_out_through_fixture(injury, fixture_date):
    """CRITICAL gate (see module docstring): only an indefinite absence
    (no expected_return_date) or a return date strictly AFTER the
    fixture counts as "out for this match." A return date on/before the
    fixture means the data says they should be back in time - not
    flagged, even though they're still on the injury list today."""
    ret = injury["expected_return_date"]
    if ret is None:
        return True
    return ret > fixture_date


def run_scan():
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    state = load_state()
    state.setdefault("flags", {})

    board = fetch_betr_soccer_board()
    counters = {"events_scanned": len(board), "player_lines_checked": 0, "new_flags": 0, "already_flagged": 0}

    # --- Detection pass ---------------------------------------------------
    injuries_by_team = {}
    for eid, ev in board.items():
        fixture_dt = _parse_betr_date(ev["date"])
        fixture_date = fixture_dt.date() if fixture_dt else None
        for team_name, players in ev["teams"].items():
            if team_name not in injuries_by_team:
                injuries_by_team[team_name] = get_team_injuries_cached(team_name, today_str)
            inj_by_norm = {r["normalized_name"]: r for r in injuries_by_team[team_name]}

            for norm, pdata in players.items():
                counters["player_lines_checked"] += 1
                injury = inj_by_norm.get(norm)
                if injury is None or fixture_date is None:
                    continue
                if not _confirmed_out_through_fixture(injury, fixture_date):
                    continue

                flag_id = f"{eid}:{norm}"
                if flag_id in state["flags"]:
                    counters["already_flagged"] += 1
                    continue

                other_team = next((t for t in ev["teams"] if t != team_name), None)
                flag = {
                    "flag_id": flag_id, "event_id": eid, "league": ev["league"],
                    "name": pdata["name"], "normalized_name": norm, "team": team_name,
                    "opponent": other_team, "event_date": ev["date"],
                    "reason": injury["reason"], "since": injury["since"], "expected_return": injury["expected_return"],
                    "markets": sorted(pdata["markets"]), "first_seen_utc": now.isoformat(),
                    "resolved": False, "grade": None, "resolution": None, "discord_message_id": None,
                }
                state["flags"][flag_id] = flag
                counters["new_flags"] += 1
                _log_event({"ts": now.isoformat(), "type": "new_flag", **{k: v for k, v in flag.items() if k != "markets"}, "markets": flag["markets"]})
                _discord_post(flag)

    # --- Grading pass: any unresolved flag whose fixture is old enough ----
    graded = 0
    for flag in state["flags"].values():
        if flag.get("resolved"):
            continue
        fixture_dt = _parse_betr_date(flag["event_date"])
        if fixture_dt is None or now < fixture_dt + timedelta(hours=GRADE_AFTER_HOURS):
            continue

        home_id = espn_soccer.match_espn_team_id(flag["league"], flag["team"])
        away_id = espn_soccer.match_espn_team_id(flag["league"], flag["opponent"]) if flag["opponent"] else None
        espn_event = espn_soccer.find_espn_event(flag["league"], home_id, away_id, fixture_dt.strftime("%Y-%m-%d")) if home_id and away_id else None
        squad = espn_soccer.get_match_squad_names(flag["league"], espn_event["id"]) if espn_event else {}

        if not squad:
            # Give it more time before declaring unresolved - only bail
            # out once the match is old enough that ESPN should certainly
            # have posted it by now (a week is generous).
            if now < fixture_dt + timedelta(days=7):
                continue
            flag["resolved"], flag["grade"], flag["resolution"] = True, None, "unresolved_no_espn_data"
        elif flag["normalized_name"] in squad:
            flag["resolved"], flag["grade"], flag["resolution"] = True, "loss", "appeared_in_squad"
        else:
            flag["resolved"], flag["grade"], flag["resolution"] = True, "win", "confirmed_did_not_play"

        graded += 1
        _log_event({"ts": now.isoformat(), "type": "graded", "flag_id": flag["flag_id"], "name": flag["name"], "grade": flag["grade"], "resolution": flag["resolution"]})
        _discord_edit(flag)

    save_state(state)

    wins = sum(1 for f in state["flags"].values() if f.get("grade") == "win")
    losses = sum(1 for f in state["flags"].values() if f.get("grade") == "loss")
    unresolved_no_data = sum(1 for f in state["flags"].values() if f.get("resolved") and f.get("grade") is None)
    active = sum(1 for f in state["flags"].values() if not f.get("resolved"))

    summary = {
        "scanned_at_utc": now.isoformat(), "counters": counters, "graded_this_run": graded,
        "active_unresolved_flags": active, "total_tracked_flags": len(state["flags"]),
        "record": {"wins": wins, "losses": losses, "unresolved_no_espn_data": unresolved_no_data},
    }
    return summary


def main():
    summary = run_scan()
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
