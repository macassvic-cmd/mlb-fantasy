"""
NFL/NBA/NHL DNS (did-not-play) detector - Phase 0, opened 2026-09-09,
covering the same betting logic as stale_lines.py (MLB) and
soccer_dns.py (soccer): a player has a live prop line but is actually
confirmed out, so the book hasn't caught up yet. See those modules'
docstrings for the full framing.

Parametrized by `league` ("nfl"|"nba"|"nhl" - see
scrapers/espn_pro_leagues.LEAGUE_CONFIG) rather than three near-
duplicate files, since all three share the same detection shape: no
per-fixture lineup/pairing needed (unlike soccer, which pairs both
sides of a fixture for grading) - just "is this player's team-injury-
report entry confirmed out as of right now."

DETECTION: for every player with an open prop line, cross-reference
their team's ESPN injury log (scrapers/espn_pro_leagues.
get_team_injuries_cached - see that module's docstring for why it has
to dedupe a time-ordered status log down to each player's latest entry
first). Same CRITICAL gate as soccer_dns.py: an expected return date
strictly AFTER the event date means confirmed out for this game; a
return date on/before it means they're expected back in time - not
flagged even though they're still listed. No return date at all
(indefinite/season-ending) always flags.

GRADING DEFERRED: soccer_dns.py's grading pass confirms a flag by
checking whether the player appeared in either team's matchday squad,
via a completed match's ESPN summary/boxscore. That response shape has
NOT been verified yet for NFL/NBA/NHL - the 2026-09-09 build window fell
right at these leagues' season openers, before any completed game
existed to check the real payload against (see espn_pro_leagues.py's
own docstring - team lists and the injuries endpoint ARE confirmed
live). Building grading against an unverified shape risks the same
class of silent-wrong-result bug the pitcher filter already caused once
this project - flags are tracked with resolved=False indefinitely for
now; grading is a follow-up once a real completed game can be checked.

DISCORD: one message per flag at detection, digest of everything
currently live - same shape/limits as soccer_dns.py's, reused directly.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

from scrapers import espn_pro_leagues

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DISCORD_WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"

STATE_DIR_TEMPLATE = os.path.join("data", "pro_league_dns", "{league}")

DISCORD_EMBED_DESC_LIMIT = 3800
DISCORD_EMBEDS_PER_MSG = 10
DISCORD_MSG_CHAR_BUDGET = 5500


def _state_paths(league):
    d = STATE_DIR_TEMPLATE.format(league=league)
    return d, os.path.join(d, "state.json"), os.path.join(d, "events.jsonl")


def _discord_webhook_url():
    return os.environ.get(DISCORD_WEBHOOK_ENV_VAR)


def _parse_iso_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_state(league):
    _, state_path, _ = _state_paths(league)
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            return json.load(f)
    return {"flags": {}}


def save_state(league, state):
    state_dir, state_path, _ = _state_paths(league)
    os.makedirs(state_dir, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)


def _log_event(league, event):
    state_dir, _, events_path = _state_paths(league)
    os.makedirs(state_dir, exist_ok=True)
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def _confirmed_out_through_fixture(injury, fixture_date):
    """Identical gate to soccer_dns._confirmed_out_through_fixture - an
    indefinite absence or a return date strictly after the fixture
    counts as out for this game; a return date on/before it doesn't."""
    ret = injury["expected_return_date"]
    if not ret:
        return True
    try:
        ret_date = datetime.fromisoformat(ret).date()
    except ValueError:
        return True
    return ret_date > fixture_date


def _flag_embed(flag, league):
    emoji = espn_pro_leagues.LEAGUE_CONFIG[league]["emoji"]
    fields = [
        {"name": "Team", "value": flag["team"], "inline": True},
        {"name": "League", "value": league.upper(), "inline": True},
        {"name": "Game date", "value": flag["event_date"][:10], "inline": True},
        {"name": "Status", "value": flag["status"], "inline": False},
        {"name": "Since", "value": flag["since"] or "unknown", "inline": True},
        {"name": "Expected return", "value": flag["expected_return"] or "Indefinite (no date set)", "inline": True},
        {"name": "Live markets", "value": ", ".join(flag["markets"]) or "(none)", "inline": False},
    ]
    return {
        "title": f"{emoji} {flag['name']} — {flag['team']}",
        "description": f"Live {league.upper()} prop line, confirmed out per ESPN's injury report.",
        "color": 0xE67E22,
        "fields": fields,
    }


def _discord_post(flag, league):
    webhook_url = _discord_webhook_url()
    if not webhook_url:
        return False
    try:
        resp = requests.post(f"{webhook_url}?wait=true", json={"embeds": [_flag_embed(flag, league)]}, timeout=15)
        resp.raise_for_status()
        flag["discord_message_id"] = resp.json().get("id")
        return True
    except Exception as e:
        logger.warning(f"Discord POST failed for {flag['name']}: {e}")
        return False


def _digest_line(flag):
    ret = flag["expected_return"] or "indefinite"
    fixture = flag["event_date"][:10] if flag.get("event_date") else "?"
    markets = ", ".join(flag["markets"])
    return (f"**{flag['name']}** ({flag['team']}) on {fixture} — "
            f"{flag['status']}, since {flag['since'] or 'unknown'}, return: {ret} — _{markets}_")


def _chunk_lines(lines, limit):
    chunks, current, current_len = [], [], 0
    for line in lines:
        added_len = len(line) + 1
        if current and current_len + added_len > limit:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += added_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def build_digest_embeds(state, league):
    active = [f for f in state.get("flags", {}).values() if not f.get("resolved")]
    if not active:
        return []
    active.sort(key=lambda f: f.get("since") or "")
    emoji = espn_pro_leagues.LEAGUE_CONFIG[league]["emoji"]
    lines = [_digest_line(f) for f in active]
    chunks = _chunk_lines(lines, DISCORD_EMBED_DESC_LIMIT)
    embeds = []
    for i, chunk in enumerate(chunks):
        title = f"{emoji} {league.upper()} — {len(active)} live" if i == 0 else f"{emoji} {league.upper()} (cont.)"
        embeds.append({"title": title, "description": chunk, "color": 0x3498DB})
    return embeds


def send_digest(embeds, league):
    webhook_url = _discord_webhook_url()
    if not webhook_url:
        logger.warning("DISCORD_WEBHOOK_URL not set - digest not sent.")
        return 0
    if not embeds:
        logger.info(f"{league.upper()} digest: nothing currently live - skipping send.")
        return 0

    batches, current, current_chars = [], [], 0
    for embed in embeds:
        embed_chars = len(embed.get("title", "")) + len(embed.get("description", ""))
        if current and (len(current) >= DISCORD_EMBEDS_PER_MSG or current_chars + embed_chars > DISCORD_MSG_CHAR_BUDGET):
            batches.append(current)
            current, current_chars = [], 0
        current.append(embed)
        current_chars += embed_chars
    if current:
        batches.append(current)

    sent = 0
    for i, batch in enumerate(batches):
        payload = {"embeds": batch}
        if i == 0:
            payload["content"] = f"**{league.upper()} DNS — Daily Digest**"
        try:
            resp = requests.post(webhook_url, json=payload, timeout=15)
            resp.raise_for_status()
            sent += 1
        except Exception as e:
            logger.warning(f"{league.upper()} digest POST failed for batch {i+1}/{len(batches)}: {e}")
    return sent


def run_scan(entries, league):
    """entries: board_loader.to_pro_league_entries()'s output - flat
    [{name, normalized_name, team, event_date_utc, markets}], same shape
    as to_mlb_betr_entries. No live-API fallback (unlike stale_lines/
    soccer_dns's Betr fallback) - this detector never had one; JSON is
    the only input from day one."""
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    state = load_state(league)
    state.setdefault("flags", {})

    counters = {"player_lines_checked": 0, "no_team_match": 0, "new_flags": 0, "already_flagged": 0}
    injuries_by_team_id = {}

    for entry in entries:
        counters["player_lines_checked"] += 1
        fixture_dt = _parse_iso_date(entry["event_date_utc"])
        fixture_date = fixture_dt.date() if fixture_dt else None
        if fixture_date is None:
            continue

        team_id = espn_pro_leagues.match_espn_team_id(league, entry["team"])
        if not team_id:
            counters["no_team_match"] += 1
            continue

        if team_id not in injuries_by_team_id:
            injuries_by_team_id[team_id] = espn_pro_leagues.get_team_injuries_cached(league, team_id, today_str)
        inj_by_norm = {r["normalized_name"]: r for r in injuries_by_team_id[team_id]}

        injury = inj_by_norm.get(entry["normalized_name"])
        if injury is None or not _confirmed_out_through_fixture(injury, fixture_date):
            continue

        flag_id = f"{entry['normalized_name']}:{entry['event_date_utc']}"
        if flag_id in state["flags"]:
            counters["already_flagged"] += 1
            continue

        flag = {
            "flag_id": flag_id, "name": entry["name"], "normalized_name": entry["normalized_name"],
            "team": entry["team"], "event_date": entry["event_date_utc"],
            "status": injury["status"], "since": injury["since"], "expected_return": injury["expected_return_date"],
            "markets": sorted(entry["markets"]), "first_seen_utc": now.isoformat(),
            "resolved": False, "grade": None, "resolution": None, "discord_message_id": None,
        }
        state["flags"][flag_id] = flag
        counters["new_flags"] += 1
        _log_event(league, {"ts": now.isoformat(), "type": "new_flag", **{k: v for k, v in flag.items() if k != "markets"}, "markets": flag["markets"]})
        _discord_post(flag, league)

    save_state(league, state)

    active = sum(1 for f in state["flags"].values() if not f.get("resolved"))
    digest_embeds = build_digest_embeds(state, league)
    digest_sent = send_digest(digest_embeds, league)

    return {
        "league": league, "scanned_at_utc": now.isoformat(), "counters": counters,
        "active_unresolved_flags": active, "total_tracked_flags": len(state["flags"]),
        "digest": {"embeds": len(digest_embeds), "messages_sent": digest_sent},
    }
