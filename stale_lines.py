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

CORRECTION (2026-09-01): flags used to also be created when a team's
lineup wasn't posted yet ("lineup_unavailable"), on the theory that a
later poll would re-check it once the real lineup appeared. In practice
this produced ZERO signal: Betr posts hitter props for a team's likely
full roster hours before lineups post, so "has a posted Betr line, no
lineup posted yet" is true of nearly every rostered player on a normal
day - not a scratch signal at all. Empirically, ALL 300 of this
detector's first real flags were created this way, and every one that
resolved (17/17) was a LOSS - completely ordinary starters whose props
simply posted first. Pass 1 now skips creating a flag entirely when the
lineup isn't posted yet (see run_poll) - a flag only exists once the
real lineup card is checked and the player is confirmed absent from it.
The lineup_unavailable category/upgrade path below is kept only to
resolve flags that were already open before this correction shipped.

FALSE-POSITIVE (loss) PROTECTION - i.e., "our lineup source was wrong":
  1. "lineup_unavailable" category (LEGACY - see correction above, no
     longer created going forward): if the team's side of the schedule
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

MEASUREMENT: independent of DNS grading, every active flag is also
checked at 5/15/30 minutes past first-seen for whether Betr still has
ANY line posted for that player. This answers a separate question from
grading: does the stale line get pulled within our alerting window, or
does it linger long enough to be worth acting on? Runs unconditionally,
regardless of whether Discord notifications are configured.

DISCORD NOTIFICATIONS (Phase 3, added 2026-09-01): informational, not
gated on the measurement data above - notify as soon as a flag is
confirmed, don't wait to see whether the line survives. Only
category="not_in_lineup" alerts; "lineup_unavailable" means the lineup
card genuinely hasn't posted yet, which isn't actionable and would flood
the channel with noise that resolves itself on a later poll (a flag
created as lineup_unavailable that's later confirmed absent from a
posted lineup DOES still alert, at the point of that upgrade - see
run_poll's grading pass). Each flag gets ONE Discord message that is
EDITED in place at each 5/15/30-minute checkpoint (via the webhook's
`?wait=true` create + `.../messages/{id}` PATCH pattern - no extra
Discord app/bot/secret needed beyond the webhook URL itself), rather
than a new message per checkpoint - keeps a fast-moving pre-game window
from turning into 4 messages per player. Configured via the
DISCORD_WEBHOOK_URL environment variable (a GitHub Actions secret, never
committed - see .github/workflows/stale_lines.yml); if unset, every
notification call is a silent no-op and log-only measurement is
unaffected.

EARLY (PRE-LINEUP) SIGNALS (Phase 4, added 2026-09-01), LOG-ONLY - no
Discord wiring for these yet, deliberately, until lead time and accuracy
are known: the lineup-based flag above only fires once MLB's lineup card
posts, typically ~3h before first pitch - by then Betr has often already
pulled the line itself, so there's no real window to act. The actual
edge is earlier: knowing a player's out before the lineup confirms it.
Two structured MLB Stats API sources, checked every poll for any player
who currently has a posted Betr line (both stored in
state["early_signals"], keyed distinctly from state["flags"] so they
can't collide with or double-count the lineup-based flags):
  1. Transactions (/api/v1/transactions) - classify_transaction() keeps
     only unambiguous "removed from an active MLB roster" types (see its
     docstring for the exact typeCode/description rules mined from a
     14-day sample) - IL placements, options to the minors, DFAs,
     releases. Explicitly excludes activations/recalls/contract
     selections (the opposite signal). Deduped by the transaction's own
     unique id, so each real transaction only ever fires once.
  2. 40-man roster status (/teams/{id}/roster?rosterType=40Man) - unlike
     the 26-man "active" roster used elsewhere, this includes IL/
     optioned players with a status code (D10/D15/D60/RM/...). Any
     Betr-lined player whose CURRENT status isn't "A" (Active) is a
     high-confidence signal. Deduped by (player_id, status_code), so a
     long-standing IL stint doesn't re-fire every poll - only a genuine
     status transition does.
A third source (RotoWire's public MLB news RSS feed,
rotowire.com/rss/news.php?sport=MLB) was investigated but NOT built -
confirmed publicly accessible with no auth, well-formed RSS, and a
fairly consistent "PlayerName: headline" title pattern with real
pre-lineup signal already visible in spot checks - but it's free text
requiring a real keyword/regex classifier (not a clean field), so it's
left for a later pass once the two structured sources above have proven
out.

Lead time is the whole point of this phase: _resolve_early_signals runs
as its own decoupled pass (same reasoning as the grading pass above -
can't be nested inside a per-entry loop) over every unresolved early
signal, and once the same player+game either gets a standard
not_in_lineup flag OR is confirmed in the real lineup, records the gap
in minutes between the early signal firing and that lineup-based
confirmation (or notes if the early signal turned out wrong - the
player started anyway).
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

# Same pattern scrapers/weather.py already uses for OPENWEATHER_API_KEY -
# a no-op in GitHub Actions (no .env file there; DISCORD_WEBHOOK_URL comes
# through as an explicit env var from the workflow YAML either way) but
# required for a standalone local invocation of this file specifically
# (`python stale_lines.py --test-discord`) to pick up .env's webhook at
# all. stale_lines_local.py already calls load_dotenv() itself before
# importing this module, so this is redundant but harmless there -
# without it here too, `python stale_lines.py --test-discord` run
# directly (not through stale_lines_local.py) would silently no-op.
load_dotenv()

from scrapers.betr import fetch_betr_hitter_lines_with_context, normalize_name
from scrapers.mlb_api import (
    get_games,
    get_team_abbreviations,
    get_active_roster,
    get_live_feed_batting_orders,
    get_forty_man_roster,
    get_recent_transactions,
)

logger = logging.getLogger(__name__)

STATE_DIR = os.path.join("data", "stale_lines")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
EVENTS_LOG_PATH = os.path.join(STATE_DIR, "events.jsonl")

# Lowered from 30 to 5 on 2026-09-01 - the Tyler Stephenson miss that day
# was diagnosed as a GitHub Actions cron gap (103 minutes with zero polls),
# not this gate (his lineup posted 3h16m-4h59m before first pitch, nowhere
# near either threshold) - but 30 was still wrong on its own terms for a
# strategy where a human can act on a 5-7 minute window. Lowering it now
# that the local runner (stale_lines_local.py) polls every 30s makes a
# 5-minute-out flag actually reachable in practice, not just in theory.
MIN_MINUTES_TO_FIRST_PITCH = 5

# Extended 2026-09-02: the first cohort of real not_in_lineup flags showed
# 13/14 (93%) already gone by the OLD first checkpoint (5 min) - meaning the
# entire actionable window was invisible to this measurement. Sub-5-minute
# buckets close that gap so the record shows whether Betr pulls in 30s, 2
# min, or 4 min - the actual question that decides whether a 30s-poll alert
# has any usable window at all. 0.5 min = 30s matches the local runner's own
# poll cadence (stale_lines_local.py); finer than that isn't measurable
# since it's faster than we poll.
CHECK_INTERVALS_MIN = [0.5, 1, 2, 3, 5, 15, 30]

DISCORD_WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"
_PACIFIC = ZoneInfo("America/Los_Angeles")

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


def _discord_webhook_url():
    return os.environ.get(DISCORD_WEBHOOK_ENV_VAR)


def _format_game_time_pt(game_time_utc):
    # %-I (no leading zero) is a glibc-only strftime extension - Windows
    # needs %#I instead. Same split report.game_time_pt already uses.
    try:
        pt = _parse_iso(game_time_utc).astimezone(_PACIFIC)
        return pt.strftime("%#I:%M %p PT") if os.name == "nt" else pt.strftime("%-I:%M %p PT")
    except Exception as e:
        logger.warning(f"_format_game_time_pt failed for {game_time_utc!r}: {e}")
        return game_time_utc


def _format_markets(markets):
    return ", ".join(f"{k} {v}" for k, v in sorted(markets.items())) or "(none)"


def _flag_embed(flag):
    """Build the current Discord embed for a flag - same shape whether
    this is the first post or a later edit; the "Checkpoints" field is
    whatever's accumulated in flag["discord_checkpoint_lines"] so far."""
    return {
        "title": f"\U0001F6A8 {flag['name']} — {flag['team']}",
        "description": "Posted line(s), not in the confirmed starting lineup.",
        "color": 0xE67E22,
        "fields": [
            {"name": "Team", "value": flag["team"], "inline": True},
            {"name": "Game time", "value": f"{_format_game_time_pt(flag['game_time_utc'])} (~{flag['minutes_to_first_pitch_at_flag']:.0f} min out when flagged)", "inline": True},
            {"name": "Category", "value": flag["category"], "inline": True},
            {"name": "Markets", "value": _format_markets(flag["all_markets"]), "inline": False},
            {"name": "Checkpoints", "value": "\n".join(flag["discord_checkpoint_lines"]) or "_pending..._", "inline": False},
        ],
    }


def _discord_post_new_flag(flag):
    """POST the flag's initial embed, storing the returned message_id on
    the flag so later checkpoints can PATCH the SAME message instead of
    posting a new one each time. No-op (returns False) if
    DISCORD_WEBHOOK_URL isn't set, or if the POST fails for any reason -
    a Discord outage must never break flag detection/logging."""
    webhook_url = _discord_webhook_url()
    if not webhook_url:
        return False
    try:
        resp = requests.post(f"{webhook_url}?wait=true", json={"embeds": [_flag_embed(flag)]}, timeout=15)
        resp.raise_for_status()
        flag["discord_message_id"] = resp.json().get("id")
        return True
    except Exception as e:
        logger.warning(f"Discord webhook POST failed for {flag['name']}: {e}")
        return False


def _discord_edit_checkpoint(flag):
    """PATCH the flag's existing Discord message with an updated
    Checkpoints field. No-op if there's no message to edit (webhook
    unset, or the original POST failed) or if the PATCH itself fails."""
    webhook_url = _discord_webhook_url()
    if not webhook_url or not flag.get("discord_message_id"):
        return False
    try:
        resp = requests.patch(
            f"{webhook_url}/messages/{flag['discord_message_id']}",
            json={"embeds": [_flag_embed(flag)]}, timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Discord webhook PATCH failed for {flag['name']}: {e}")
        return False


def post_system_alert(title, description, color=0xE74C3C):
    """Generic (non-flag) Discord alert - used by stale_lines_local.py
    (poll crashed) and stale_lines_watchdog.py (heartbeat stale), not by
    the flag-detection logic above. Always a fresh message (no
    POST+edit pairing like _discord_post_new_flag/_discord_edit_
    checkpoint - a crash/stall alert is a one-off event, not something
    that gets updated over time). Same no-op-if-unset, never-raise
    contract as the flag-alert functions - a Discord outage on TOP of
    whatever this is alerting about must never take down the caller."""
    webhook_url = _discord_webhook_url()
    if not webhook_url:
        return False
    try:
        resp = requests.post(webhook_url, json={"embeds": [{"title": title, "description": description, "color": color}]}, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.warning(f"Discord system-alert POST failed: {e}")
        return False


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
    """The game (yesterday, today, or tomorrow's schedule) that team_id is playing in,
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


def _update_lineup_posted_index(state, lineup_index, now):
    """state["lineup_posted_at"][f"{game_pk}:{side}"] = the ISO timestamp of
    the FIRST poll that ever observed that side's lineup as posted - written
    once, never overwritten. Added 2026-09-02 so a flag/signal created
    against an already-posted lineup can report how long WE took to notice
    the posting (our own poll-cadence latency), separate from how long Betr
    takes to react once we do. Necessarily approximate right after a
    process (re)start or the first poll of a new game day: if the lineup
    was already posted the very first time this index sees that game side,
    the true posting time could be anywhere before that first observation -
    there is no way to backdate it after the fact."""
    posted_index = state.setdefault("lineup_posted_at", {})
    for game_pk, idx in lineup_index.items():
        for side in ("home", "away"):
            if idx[f"{side}_posted"]:
                key = f"{game_pk}:{side}"
                if key not in posted_index:
                    posted_index[key] = now.isoformat()


# ---------------------------------------------------------------------------
# Early (pre-lineup) signals - Phase 4, log-only. See module docstring.
# ---------------------------------------------------------------------------

# Mined from a 14-day sample of /api/v1/transactions (550 transactions,
# session notes have the full typeCode breakdown): these four typeCodes
# unambiguously mean "removed from an active MLB roster" with no
# legitimate opposite-direction usage observed - OPT (optioned to
# minors), OUT (sent outright), DES (designated for assignment), REL
# (released). "SC" (Status Change, the largest single bucket) covers
# BOTH IL placements AND IL activations under the same code, so it needs
# description text matched instead of a bare code check.
TRANSACTION_OUT_TYPE_CODES = {"OPT", "OUT", "DES", "REL"}


def classify_transaction(t):
    """Return a short category string if this transaction is a
    high-confidence pre-lineup OUT signal, or None if it's noise or the
    OPPOSITE signal (an activation/recall/contract-selection, which
    means the player is now MORE available, not less - deliberately
    checked first and excluded even if the description also happens to
    contain "injured list", e.g. "activated ... from the injured list")."""
    code = t.get("typeCode")
    desc = (t.get("description") or "").lower()
    if any(k in desc for k in ("activated", "reinstated", "recalled", "selected the contract")):
        return None
    if code in TRANSACTION_OUT_TYPE_CODES:
        return "roster_move_out"
    if code == "SC" and "injured list" in desc and ("placed" in desc or "transferred" in desc):
        return "injured_list"
    return None


def _transaction_team_id(t):
    """The MLB team_id the player is LEAVING. An option/outright/DFA/
    release has both fromTeam (the MLB team) and toTeam (the minor
    league affiliate, or None for a release) - fromTeam is what we want.
    An IL placement has only toTeam (the MLB team doing the placing,
    which IS the player's current team) and no fromTeam at all."""
    if t.get("fromTeam"):
        return t["fromTeam"]["id"]
    if t.get("toTeam"):
        return t["toTeam"]["id"]
    return None


def _new_early_signal(key, source, category, name, norm, team_abbr, games, betr_entry, now, extra):
    """Shared shape for both early-signal sources below - resolves the
    player's upcoming game (if possible, for later lead-time comparison
    against the corresponding lineup-based flag) using the exact same
    team+event-time matching as the main detector."""
    event_dt = _parse_iso(betr_entry["event_date_utc"]) if betr_entry else None
    team_abbrevs = get_team_abbreviations()
    team_id = team_abbrevs.get(team_abbr) or team_abbrevs.get(BETR_TEAM_ABBR_OVERRIDES.get(team_abbr))
    game, is_home = (None, None)
    if team_id is not None and event_dt is not None:
        game, is_home = _find_game_for_team(games, team_id, event_dt)
    signal = {
        "flag_id": key,
        "source": source,
        "category": category,
        "name": name,
        "normalized_name": norm,
        "team": team_abbr,
        "game_pk": game["gamePk"] if game else None,
        "is_home": is_home,
        "game_time_utc": game["gameDate"] if game else None,
        "first_seen_utc": now.isoformat(),
        "resolved": False,
        "outcome": None,
        "lineup_lag_minutes": None,
        # t=0 baseline, added 2026-09-02: True by construction (this signal
        # only exists because betr_entry was live in THIS poll's Betr
        # fetch - see check_transactions/check_roster_status's own gate),
        # but recorded explicitly with its own timestamp rather than left
        # implicit, so _measure_early_signal_checkpoints below has a real
        # anchor point instead of assuming t=0 is always True.
        "checks": [{
            "elapsed_min": 0,
            "checked_at_utc": now.isoformat(),
            "still_has_any_market": True,
            "still_posted_markets": betr_entry["markets"] if betr_entry else None,
        }],
    }
    signal.update(extra)
    return signal


def check_transactions(now, betr_by_name, games, state):
    """Poll /api/v1/transactions over a short rolling window and create a
    new early_signals entry for any classify_transaction()-positive move
    involving a player who currently has a posted Betr line. Deduped by
    the transaction's own unique id - a real transaction is a one-time
    event, never re-fires."""
    start = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    try:
        txns = get_recent_transactions(start, end)
    except Exception as e:
        logger.warning(f"get_recent_transactions failed: {e}")
        return []

    state.setdefault("early_signals", {})
    new_signals = []
    for t in txns:
        category = classify_transaction(t)
        if category is None:
            continue
        person = t.get("person") or {}
        name = person.get("fullName")
        if not name:
            continue
        norm = normalize_name(name)
        betr_entry = betr_by_name.get(norm)
        if betr_entry is None:
            continue  # no posted Betr line - not relevant to this detector

        key = f"transaction:{t.get('id')}"
        if key in state["early_signals"]:
            continue

        signal = _new_early_signal(
            key, "transaction", category, name, norm, betr_entry["team"], games, betr_entry, now,
            {"transaction_id": t.get("id"), "transaction_date": t.get("date"), "description": t.get("description")},
        )
        state["early_signals"][key] = signal
        new_signals.append(signal)
        _log_event({"ts": now.isoformat(), "type": "early_signal", **signal})
    return new_signals


def check_roster_status(now, betr_by_name, team_abbrevs, games, state):
    """Check the 40-man roster status of every team with at least one
    Betr-lined player today, and create a new early_signals entry for
    any such player whose CURRENT status isn't "A" (Active). Deduped by
    (player_id, status_code) - a long-standing IL stint doesn't re-fire
    every poll, only a genuine status transition (e.g. D10 -> D60) does."""
    state.setdefault("early_signals", {})
    new_signals = []
    checked_teams = set()
    for betr_entry in betr_by_name.values():
        team_abbr = betr_entry["team"]
        team_id = team_abbrevs.get(team_abbr) or team_abbrevs.get(BETR_TEAM_ABBR_OVERRIDES.get(team_abbr))
        if team_id is None or team_id in checked_teams:
            continue
        checked_teams.add(team_id)
        try:
            roster = get_forty_man_roster(team_id)
        except Exception as e:
            logger.warning(f"get_forty_man_roster({team_id}) failed: {e}")
            continue
        for p in roster:
            if p["status_code"] == "A" or not p["status_code"]:
                continue
            norm = normalize_name(p["name"])
            player_betr_entry = betr_by_name.get(norm)
            if player_betr_entry is None:
                continue

            key = f"status:{p['player_id']}:{p['status_code']}"
            if key in state["early_signals"]:
                continue

            signal = _new_early_signal(
                key, "roster_status", f"status_{p['status_code']}", p["name"], norm, team_abbr, games, player_betr_entry, now,
                {"status_code": p["status_code"], "status_desc": p["status_desc"], "note": p.get("note")},
            )
            state["early_signals"][key] = signal
            new_signals.append(signal)
            _log_event({"ts": now.isoformat(), "type": "early_signal", **signal})
    return new_signals


def _measure_early_signal_checkpoints(now, betr_by_name, live_names_now, state):
    """Same 5/15/30(+sub-5-minute) 'is Betr's line still posted' measurement
    run_poll's grading pass already does for lineup-based flags, applied to
    early_signals - added 2026-09-02. Without this, an early signal had no
    way to show whether it buys real time before Betr also reacts, which is
    the entire point of comparing this source against the lineup-based
    flag. Stops once a signal resolves, same as the flag version - matches
    CHECK_INTERVALS_MIN exactly so the two are directly comparable."""
    for sig in state.get("early_signals", {}).values():
        if sig.get("resolved"):
            continue
        sig.setdefault("checks", [])
        first_seen = _parse_iso(sig["first_seen_utc"])
        elapsed_min = (now - first_seen).total_seconds() / 60
        done_thresholds = {c["elapsed_min"] for c in sig["checks"]}
        still_live_entry = betr_by_name.get(sig["normalized_name"])
        still_has_any_market = sig["normalized_name"] in live_names_now
        for threshold in CHECK_INTERVALS_MIN:
            if threshold in done_thresholds or elapsed_min < threshold:
                continue
            check = {
                "elapsed_min": threshold,
                "checked_at_utc": now.isoformat(),
                "still_has_any_market": still_has_any_market,
                "still_posted_markets": (still_live_entry["markets"] if still_live_entry else None),
            }
            sig["checks"].append(check)
            _log_event({"ts": now.isoformat(), "type": "early_signal_checkpoint", "flag_id": sig["flag_id"], "name": sig["name"], **check})


def _resolve_early_signals(now, state, lineup_index):
    """Decoupled pass (same reasoning as the main grading pass - can't be
    nested inside a per-entry loop) over every unresolved early_signals
    entry: once its game_pk/is_home is known and that game's lineup index
    is available, check whether:
      (a) the player shows up in the real lineup anyway -> "player_started_
          anyway" (the early signal was wrong)
      (b) a matching standard flag now exists for the same game_pk+player
          AND its category is genuinely "not_in_lineup" (a confirmed
          absence, not a still-pending "lineup_unavailable" placeholder -
          matching against one of those produced nonsense negative lag
          values against the real legacy backlog before this check was
          added) AND it was first seen AFTER this signal (a match that
          predates the signal isn't the signal buying any lead time) ->
          "confirmed_by_lineup_flag", with lineup_lag_minutes recording
          how much earlier the signal fired; a not_in_lineup match that
          predates or ties the signal instead resolves as
          "lineup_flag_predates_signal" (directionally right, but not a
          genuine early win)
      (c) the game has started with no qualifying match ever appearing ->
          "confirmed_not_started_no_flag" if the lineup posted (signal was
          directionally right, nothing to compare against - e.g.
          MIN_MINUTES_TO_FIRST_PITCH filtered out a standard flag) or
          "unresolved_lineup_never_posted" if it never did."""
    for key, sig in state.get("early_signals", {}).items():
        if sig.get("resolved"):
            continue
        if sig.get("game_pk") is None:
            continue  # couldn't resolve a game yet - try again next poll
        idx = lineup_index.get(sig["game_pk"])
        if idx is None:
            continue
        side_started = idx["home_started"] if sig["is_home"] else idx["away_started"]
        side_posted = idx["home_posted"] if sig["is_home"] else idx["away_posted"]
        game_dt = _parse_iso(idx["game_time_utc"])

        if sig["normalized_name"] in side_started:
            sig["resolved"] = True
            sig["outcome"] = "player_started_anyway"
            _log_event({"ts": now.isoformat(), "type": "early_signal_resolved", "flag_id": key, "outcome": sig["outcome"]})
            continue

        # Only a flag that's a GENUINE confirmed absence (category ==
        # not_in_lineup) counts as validating this early signal - a
        # match against a still-pending "lineup_unavailable" placeholder
        # (legacy flags from before 2026-09-01's correction can sit in
        # that state for hours) is not a confirmation of anything yet.
        # Also require the flag to have appeared AFTER this signal - a
        # match that predates the signal isn't the signal "buying lead
        # time," it's coincidental, and crediting it would produce
        # nonsensical negative lag values (caught exactly this case
        # against the real legacy backlog before shipping this fix).
        matching_flag = state.get("flags", {}).get(f"{sig['game_pk']}:{sig['normalized_name']}")
        if matching_flag is not None and matching_flag.get("category") == "not_in_lineup":
            matching_first_seen = _parse_iso(matching_flag["first_seen_utc"])
            sig_first_seen = _parse_iso(sig["first_seen_utc"])
            if matching_first_seen >= sig_first_seen:
                lag_min = (matching_first_seen - sig_first_seen).total_seconds() / 60
                sig["resolved"] = True
                sig["outcome"] = "confirmed_by_lineup_flag"
                sig["lineup_lag_minutes"] = round(lag_min, 1)
                _log_event({"ts": now.isoformat(), "type": "early_signal_resolved", "flag_id": key, "outcome": sig["outcome"], "lineup_lag_minutes": sig["lineup_lag_minutes"]})
            else:
                sig["resolved"] = True
                sig["outcome"] = "lineup_flag_predates_signal"
                _log_event({"ts": now.isoformat(), "type": "early_signal_resolved", "flag_id": key, "outcome": sig["outcome"]})
            continue

        if game_dt <= now:
            sig["resolved"] = True
            sig["outcome"] = "confirmed_not_started_no_flag" if side_posted else "unresolved_lineup_never_posted"
            _log_event({"ts": now.isoformat(), "type": "early_signal_resolved", "flag_id": key, "outcome": sig["outcome"]})


def run_poll():
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # Fetch yesterday+today+tomorrow, not just today+tomorrow. MLB's
    # /schedule?date= buckets a game by its LOCAL US slate date, not its
    # raw UTC calendar date - a game with a UTC gameDate of
    # "2026-09-01T01:38:00Z" (a normal PT/ET evening game) is filed under
    # date=2026-08-31, not 2026-09-01. Once `now` rolls past UTC midnight,
    # a today+tomorrow window (both computed from `now`) permanently loses
    # that game - confirmed in production: a flag whose player had
    # genuinely started sat un-gradeable for hours because its game_pk
    # fell out of the fetched window the instant UTC date rolled over.
    # yesterday+today+tomorrow guarantees any game created while still
    # >=30 min out (this module's own gate) stays visible regardless of
    # which side of UTC midnight the current poll happens to land on.
    games = _dedupe_games(get_games(yesterday_str) + get_games(today_str) + get_games(tomorrow_str))
    lineup_index = _build_lineup_index(games)
    team_abbrevs = get_team_abbreviations()
    betr_entries = fetch_betr_hitter_lines_with_context()
    live_names_now = {e["normalized_name"] for e in betr_entries}
    betr_by_name = {e["normalized_name"]: e for e in betr_entries}

    state = load_state()
    state.setdefault("flags", {})
    _update_lineup_posted_index(state, lineup_index, now)

    counters = {
        "betr_hitter_entries": len(betr_entries),
        "no_team_match": 0,
        "already_started": 0,
        "too_close_to_first_pitch": 0,
        "suppressed_by_second_source": 0,
        "skipped_lineup_not_yet_posted": 0,
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
            # NOT a flag. Corrected 2026-09-01: creating a "lineup_
            # unavailable" flag here (the original design) isn't a
            # scratch signal at all - it just means Betr has posted a
            # prop on a player before MLB's lineup card exists yet, which
            # is completely routine (Betr covers the likely full roster
            # hours ahead of lineups). Empirically, EVERY one of this
            # detector's first 300 real flags were created exactly this
            # way, and every one that has resolved so far (17/17) was a
            # LOSS - the player was, unsurprisingly, just a normal
            # starter whose prop happened to post before the lineup did.
            # See session notes for the full cohort breakdown. Skip
            # entirely here; the real signal requires the lineup to
            # already be posted with the player genuinely absent from it.
            counters["skipped_lineup_not_yet_posted"] += 1
            continue

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

        # Detection latency, added 2026-09-02: how long AFTER the real
        # lineup card first posted did our own poll cadence take to notice
        # it - separate from how long Betr then takes to react (that's what
        # the checks/checkpoints below measure). None if this game side's
        # posting predates _update_lineup_posted_index's own tracking
        # (e.g. right after a process restart) - can't backdate a posting
        # time we never observed.
        lineup_posted_key = f"{game_pk}:{'home' if is_home else 'away'}"
        lineup_first_posted_utc = state.get("lineup_posted_at", {}).get(lineup_posted_key)
        detection_latency_seconds = (
            round((now - _parse_iso(lineup_first_posted_utc)).total_seconds(), 1)
            if lineup_first_posted_utc else None
        )

        flag = {
            "flag_id": flag_id,
            "name": entry["name"],
            "normalized_name": entry["normalized_name"],
            "team": team_abbr,
            "game_pk": game_pk,
            "is_home": is_home,
            "game_time_utc": idx["game_time_utc"],
            "category": category,
            "created_as": category,
            "all_markets": entry["markets"],
            "first_seen_utc": now.isoformat(),
            "lineup_first_posted_utc": lineup_first_posted_utc,
            "detection_latency_seconds": detection_latency_seconds,
            "minutes_to_first_pitch_at_flag": round(minutes_to_first_pitch, 1),
            # t=0 baseline, added 2026-09-02: True by construction (this
            # flag only exists because `entry` was live in THIS poll's Betr
            # fetch), recorded explicitly with its own timestamp so the
            # 0.5/1/2/3/5/15/30-min checkpoints below have a real anchor
            # instead of an assumed-true starting point.
            "checks": [{
                "elapsed_min": 0,
                "checked_at_utc": now.isoformat(),
                "still_has_any_market": True,
                "still_posted_markets": entry["markets"],
            }],
            "resolved": False,
            "grade": None,
            "resolution": None,
            "discord_message_id": None,
            "discord_checkpoint_lines": [],
        }
        state["flags"][flag_id] = flag
        _log_event({
            "ts": now.isoformat(), "type": "new_flag", "flag_id": flag_id,
            "name": entry["name"], "team": team_abbr, "category": category,
            "markets": entry["markets"], "minutes_to_first_pitch": round(minutes_to_first_pitch, 1),
            "game_time_utc": idx["game_time_utc"],
        })
        # Discord alert: only category="not_in_lineup" - "lineup_unavailable"
        # isn't actionable yet and would flood the channel (see module
        # docstring). Eligibility is "not_in_lineup AND no message posted
        # yet" rather than a one-shot boolean, so a transient webhook
        # failure here gets retried automatically on the next poll (via
        # the identical check in the grading pass below) instead of
        # silently giving up.
        if category == "not_in_lineup" and flag["discord_message_id"] is None:
            _discord_post_new_flag(flag)

    # --- Pass 1b: early (pre-lineup) signals - log-only, see module
    # docstring's Phase 4 section. Independent of the lineup-based flags
    # above - these fire regardless of whether any team's lineup is
    # posted yet, which is the whole point (earlier than the lineup-based
    # flag could ever fire). --------------------------------------------
    new_transaction_signals = check_transactions(now, betr_by_name, games, state)
    new_status_signals = check_roster_status(now, betr_by_name, team_abbrevs, games, state)

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
        flag.setdefault("discord_message_id", None)
        flag.setdefault("discord_checkpoint_lines", [])

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

            # LEGACY ONLY as of 2026-09-01: pass 1 no longer creates new
            # lineup_unavailable flags at all (see the module docstring
            # and pass 1's skip logic above), so this upgrade path only
            # ever fires for flags that were already open before that
            # change shipped. Kept so those pre-existing flags still
            # resolve correctly instead of being orphaned.
            if flag["category"] == "lineup_unavailable" and side_posted:
                flag["category"] = "not_in_lineup"

            # NOT nested under the upgrade branch above - deliberately a
            # general catch-all so it also covers a flag that was ALREADY
            # not_in_lineup with no message yet: either one that existed
            # before Discord notifications were added to this module, or
            # one whose initial POST attempt failed earlier and is due for
            # a retry (see pass 1's identical check for the retry story).
            if flag["category"] == "not_in_lineup" and flag["discord_message_id"] is None:
                _discord_post_new_flag(flag)

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
        # else: this flag's game fell outside yesterday+today+tomorrow's fetched
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

            # Edit the SAME Discord message in place (not a new one) so
            # the alert reads as one player's status updating over time
            # rather than a burst of near-duplicate messages. Only for
            # flags we actually alerted on (discord_message_id set) -
            # lineup_unavailable flags never got a message to edit.
            if flag["discord_message_id"] is not None:
                status = "still live" if still_has_any_market else "no longer posted"
                icon = "✅" if still_has_any_market else "⚠️"
                flag["discord_checkpoint_lines"].append(f"{icon} {threshold} min: {status}")
                _discord_edit_checkpoint(flag)

    # --- Pass 3: measure + resolve early signals against this poll's
    # lineup index. Measurement runs first so a signal that resolves on
    # this exact poll still gets whatever checkpoint it just reached
    # recorded before resolution stops further checks. -------------------
    _measure_early_signal_checkpoints(now, betr_by_name, live_names_now, state)
    _resolve_early_signals(now, state, lineup_index)

    save_state(state)

    active_flags = [f for f in state["flags"].values() if not f.get("resolved")]
    wins = sum(1 for f in state["flags"].values() if f.get("grade") == "win")
    losses = sum(1 for f in state["flags"].values() if f.get("grade") == "loss")
    unresolved_grade = sum(1 for f in state["flags"].values() if f.get("resolved") and f.get("grade") is None)

    early_signals = state.get("early_signals", {}).values()
    confirmed_lags = [s["lineup_lag_minutes"] for s in early_signals if s.get("outcome") == "confirmed_by_lineup_flag"]
    early_signal_outcomes = {}
    for s in early_signals:
        early_signal_outcomes[s.get("outcome")] = early_signal_outcomes.get(s.get("outcome"), 0) + 1

    summary = {
        "polled_at_utc": now.isoformat(),
        "counters": counters,
        "checkpoint_events_this_poll": checkpoint_events,
        "grading_events_this_poll": grading_events,
        "active_unresolved_flags": len(active_flags),
        "total_tracked_flags": len(state["flags"]),
        "record": {"wins": wins, "losses": losses, "unresolved_lineup_never_posted": unresolved_grade},
        "early_signals": {
            "new_this_poll": len(new_transaction_signals) + len(new_status_signals),
            "new_transaction_signals": len(new_transaction_signals),
            "new_roster_status_signals": len(new_status_signals),
            "total_tracked": len(early_signals),
            "outcomes": early_signal_outcomes,
            "confirmed_lead_times_minutes": confirmed_lags,
        },
    }
    return summary


def send_test_discord_message():
    """Post a single synthetic, clearly-labeled test message through the
    EXACT same code path a real flag uses (_discord_post_new_flag ->
    _flag_embed), without touching state.json/events.jsonl at all - pure
    webhook-wiring verification, invoked via `python stale_lines.py
    --test-discord` (see .github/workflows/stale_lines.yml's
    workflow_dispatch input). Returns True/False; the caller (main())
    reports the result, but actually confirming the message *arrived* in
    the channel is on whoever set up the webhook - this process has no
    way to read Discord back."""
    now = datetime.now(timezone.utc)
    fake_flag = {
        "name": "[TEST] Webhook Wiring Check",
        "team": "N/A",
        "game_time_utc": (now + timedelta(minutes=45)).isoformat().replace("+00:00", "Z"),
        "category": "not_in_lineup",
        "all_markets": {"TEST_MARKET": 0.0},
        "minutes_to_first_pitch_at_flag": 45.0,
        "discord_checkpoint_lines": ["This is a one-off connectivity test, not a real flag - safe to ignore/delete."],
        "discord_message_id": None,
    }
    if not _discord_webhook_url():
        print("DISCORD_WEBHOOK_URL is not set - nothing to test.")
        return False
    ok = _discord_post_new_flag(fake_flag)
    if ok:
        print(f"Test message posted successfully. Discord message_id: {fake_flag['discord_message_id']}")
    else:
        print("Test message POST failed - see the warning above for the underlying error.")
    return ok


def main():
    if "--test-discord" in sys.argv:
        ok = send_test_discord_message()
        sys.exit(0 if ok else 1)
    summary = run_poll()
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
