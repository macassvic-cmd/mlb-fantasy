"""
Discord notifications + source-speed tracking for soccer DNS. Reads
already-scored Dabble soccer candidates (soccer_adapter.py) - never
recomputes scoring here. Mirrors dnp_alerts.py's MLB design (webhook via
env var only, threshold-crossing-from-below dedup, never logs the
webhook URL).

Webhook: DISCORD_SOCCER_DNS_WEBHOOK_URL (separate from MLB's
DISCORD_DNP_WEBHOOK_URL so the two products can post to different
channels) - never hardcoded, never logged, never persisted.

Alert types:
  WATCH               - dns_score 70-84, tracked only, no Discord (same
                         "tracking only" tier as MLB)
  DNS ALERT           - dns_score >= 75 and confidence >= 50
  HIGH PRIORITY        - dns_score >= 85 and confidence >= 60
  SOCCER NEWS SIGNAL   - a NEW meaningful signal materially raises the
                         score (trusted X out/doubtful, RotoWire worsens,
                         predicted XI flips START->BENCH) even before a
                         numeric threshold is crossed - item 6/11
  CONFIRMED NON-STARTER - official_status == confirmed_not_starting while
                         the Dabble prop is still live. This is the
                         CRITICAL event for soccer specifically because
                         substitute appearances don't matter for grading -
                         only whether he started - see item 11's framing.

Dedup key: date + sport + player_id + fixture_id + alert_type/threshold
(item 11's exact spec - broader than MLB's key since soccer needs the
sport segment to share a namespace safely with the MLB alert store if
ever merged).
"""

import argparse
import json
import os
import statistics
from datetime import datetime, timezone

import requests

SPORT = "soccer"
ALERTS_DIR = os.path.join("data", "soccer_dns_alerts")
WEBHOOK_ENV_VAR = "DISCORD_SOCCER_DNS_WEBHOOK_URL"

ALERT_THRESHOLDS = [60, 65, 70, 75, 80, 85, 90]
DISCORD_SEND_THRESHOLDS = {75, 85}
CRITICAL_KEY = "critical"
NEWS_SIGNAL_KEY = "news_signal"

ALERT_MIN_CONFIDENCE = {75: 50, 85: 60}

SEVERITY_LABELS = {
    70: "WATCH", 75: "DNS ALERT", 85: "HIGH PRIORITY",
    CRITICAL_KEY: "CONFIRMED NON-STARTER", NEWS_SIGNAL_KEY: "SOCCER NEWS SIGNAL",
}
SEVERITY_COLORS = {
    "WATCH": 0x95A5A6, "DNS ALERT": 0xE67E22, "HIGH PRIORITY": 0xE74C3C,
    "CONFIRMED NON-STARTER": 0x8E44AD, "SOCCER NEWS SIGNAL": 0x3498DB,
}

# A trusted-source (tier<=2) injury/bench signal, or a predicted-XI flip
# to bench, is "material" enough for an immediate news-signal alert even
# before any numeric threshold is crossed - item 6/11.
NEWS_SIGNAL_X_TRUST_MAX_TIER = 2
NEWS_SIGNAL_ROTOWIRE_WORSENING = {"hard_out", "fifty-fifty", "late-call", "fitness-test"}


FALLBACK_WEBHOOK_ENV_VAR = "DISCORD_WEBHOOK_URL"


def _webhook_url():
    """DISCORD_SOCCER_DNS_WEBHOOK_URL if set; otherwise falls back to the
    generic DISCORD_WEBHOOK_URL already configured for this deployment
    (2026-09-14: Discord made mandatory for Soccer DNS, and a dedicated
    soccer webhook was never provisioned - confirmed only the generic one
    exists) rather than treating Soccer DNS as unconfigured when a real,
    working webhook is sitting right there. Point DISCORD_SOCCER_DNS_
    WEBHOOK_URL at its own channel later if mixing with whatever else
    posts to the generic one becomes a problem."""
    return os.environ.get(WEBHOOK_ENV_VAR) or os.environ.get(FALLBACK_WEBHOOK_ENV_VAR)


def _alerts_path(date_str):
    return os.path.join(ALERTS_DIR, f"{date_str}.json")


def _load_alerts(date_str):
    path = _alerts_path(date_str)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_alerts(date_str, data):
    os.makedirs(ALERTS_DIR, exist_ok=True)
    with open(_alerts_path(date_str), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _dedup_key(date_str, player_id, fixture_id, alert_type):
    return f"{date_str}:{SPORT}:{player_id}:{fixture_id}:{alert_type}"


def _minutes_between(a_iso, b_iso):
    """None on a missing OR genuinely malformed timestamp - _parse_iso
    itself only guards against falsy input and raises on real garbage,
    which is fine for its other (trusted-source) callers but not here:
    a Dabble/candidate event_date is exactly the kind of field that can
    show up malformed (item 2, 2026-09-14's "unknown" kickoff-status
    case is built on this never raising)."""
    from stale_lines import _parse_iso
    try:
        a, b = _parse_iso(a_iso), _parse_iso(b_iso)
    except ValueError:
        return None
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60


def _build_alert_record(candidate, alert_type, date_str, now_iso, extra_reason=None):
    return {
        "player_id": candidate.get("dabble_player_id"),
        "player_name": candidate["player_name"],
        "normalized_name": candidate.get("normalized_name"),  # needed to grade against ESPN's squad list later
        "team": candidate["team"],
        "league_code": candidate.get("league_code"),  # needed to resolve the ESPN event/squad later
        "opponent": candidate.get("matchup"),
        "fixture_id": candidate.get("fixture_id"),
        "dns_score": candidate["dns_score"],
        "confidence_score": candidate["confidence_score"],
        "urgency_score": candidate["urgency_score"],
        "alert_type": alert_type,
        "alerted_at": now_iso,
        "game_start": candidate.get("event_date"),
        "minutes_to_game": _minutes_between(now_iso, candidate.get("event_date")),
        "official_status": candidate["official_status"],
        "dabble_status_raw": candidate.get("dabble_status_raw"),
        "dabble_live": True,
        "dabble_markets": [p["market"] for p in candidate.get("props", [])],
        "dabble_prop_ids": [p["prop_id"] for p in candidate.get("props", [])],
        "top_reasons": candidate.get("top_reasons", []),
        "extra_reason": extra_reason,
        # Since date of any Transfermarkt injury at alert time - item 4's
        # long-term-injury exclusion needs this to tell "already known
        # injured weeks ago" apart from "a real DNS prediction that came
        # true," so grading can report the two separately.
        "transfermarkt_injury_since": (candidate.get("transfermarkt_injury") or {}).get("since"),
        "official_started": None,
        "outcome": None,
        "long_term_injury": False,
        "graded_at": None,
        "dabble_removed_at": None,
    }


STALE_KICKOFF_KEY_PREFIX = "_stale_kickoff:"


def _kickoff_status(candidate, now_iso):
    """('upcoming'|'post_kickoff'|'unknown', minutes_to_game) - item 2
    (2026-09-14): found live that this module had NO kickoff check at
    all. A real fixture (NE @ CHI, kickoff 2026-09-13 21:30 UTC) stayed
    marked dabble_live=True on the board for ~12 hours after it actually
    started, and notify_soccer_candidates kept generating fresh
    threshold-crossing alerts for Carles Gil the whole time - the board
    data was stale, not a scoring bug, but nothing here ever checked.

    minutes_to_game > 0 means kickoff is still ahead; <= 0 means it has
    passed. 'unknown' (event_date missing/unparseable) is its OWN
    case - never treated as "still upcoming," since alerting blind
    on unparseable timing is exactly the kind of mistake this exists to
    prevent."""
    minutes_to_game = _minutes_between(now_iso, candidate.get("event_date"))
    if minutes_to_game is None:
        return "unknown", None
    return ("upcoming" if minutes_to_game > 0 else "post_kickoff"), minutes_to_game


def _record_stale_kickoff_problem(data, candidate, now_iso, minutes_to_game, reason):
    """Records a data-problem entry instead of either alerting past
    kickoff or silently doing nothing (item 2) - a fixture stuck showing
    live well after it should have kicked off, or with a game_start that
    can't even be parsed, is a real thing worth surfacing, distinct from
    a normal quiet period with nothing to report. One entry per fixture
    per day (not re-logged/re-printed on every subsequent scoring pass)."""
    pid = candidate.get("dabble_player_id")
    fixture_id = candidate.get("fixture_id")
    key = f"{STALE_KICKOFF_KEY_PREFIX}{pid}:{fixture_id}"
    if key in data:
        return
    print(f"soccer_alerts: DATA PROBLEM - {candidate.get('player_name')} ({candidate.get('matchup')}) "
          f"kickoff check failed ({reason}); suppressing real-time alerts for this fixture, not alerting blind.")
    data[key] = {
        "player_id": pid, "player_name": candidate.get("player_name"), "fixture_id": fixture_id,
        "matchup": candidate.get("matchup"), "game_start": candidate.get("event_date"),
        "detected_at": now_iso, "minutes_to_game": minutes_to_game, "reason": reason,
    }


def _severity_for(alert_type):
    return SEVERITY_LABELS.get(alert_type, "WATCH")


def _should_send_discord(candidate, alert_type):
    if alert_type == CRITICAL_KEY:
        return True
    if alert_type == NEWS_SIGNAL_KEY:
        return True
    if alert_type not in DISCORD_SEND_THRESHOLDS:
        return False
    return candidate["confidence_score"] >= ALERT_MIN_CONFIDENCE[alert_type]


def _build_embed(record, severity):
    color = SEVERITY_COLORS.get(severity, 0x95A5A6)
    matchup = record.get("opponent") or record["team"]
    fields = [
        {"name": "DNS Score", "value": str(record["dns_score"]), "inline": True},
        {"name": "Confidence", "value": str(record["confidence_score"]), "inline": True},
        {"name": "Urgency", "value": str(record["urgency_score"]), "inline": True},
        {"name": "Dabble", "value": "LIVE" if record["dabble_live"] else "REMOVED", "inline": True},
        {"name": "Lineup status", "value": record["official_status"], "inline": True},
        {"name": "Live markets", "value": ", ".join(record["dabble_markets"]) or "(none)", "inline": False},
        {"name": "Top reasons", "value": "\n".join(record["top_reasons"][:5]) or "(none)", "inline": False},
    ]
    if record.get("extra_reason"):
        fields.append({"name": "Signal", "value": record["extra_reason"], "inline": False})
    return {
        "title": f"{severity} - {record['player_name']}",
        "description": f"{record['team']} - {matchup} - {record.get('game_start') or 'time TBD'}",
        "color": color,
        "fields": fields,
        "footer": {"text": "DNS Score is a heuristic ranking, not a calibrated probability yet - see soccer_alerts.py --speed-report"},
    }


def send_discord_alert(record, severity):
    import discord_health

    webhook_url = _webhook_url()
    if not webhook_url:
        return False
    try:
        resp = requests.post(webhook_url, json={"embeds": [_build_embed(record, severity)]}, timeout=15)
        resp.raise_for_status()
        discord_health.record_attempt("realtime_alert", True, http_status=resp.status_code)
        return True
    except Exception as e:
        # Exception type + HTTP status ONLY - never str(e) (2026-09-15
        # security fix): a requests exception commonly embeds the full
        # request URL, which for a Discord webhook POST IS a bearer
        # credential. discord_health.record_attempt also redacts as a
        # backstop, but the message is built safe here in the first
        # place rather than relying solely on that second layer.
        status = getattr(getattr(e, "response", None), "status_code", None)
        print(f"soccer_alerts: Discord webhook POST failed for {record['player_name']}: {type(e).__name__}")
        discord_health.record_attempt("realtime_alert", False, http_status=status,
                                       error=f"{type(e).__name__} (HTTP {status})")
        return False


def _detect_news_signal(candidate, prior_entry):
    """Material new signal since the last check - item 6/11: trusted X
    out/doubtful, RotoWire worsening, or predicted XI flipping
    START->BENCH. Returns a reason string or None."""
    x_hit = candidate.get("x_signal")
    if x_hit and x_hit.get("author_trust_tier", 99) <= NEWS_SIGNAL_X_TRUST_MAX_TIER and x_hit.get("extracted_injury"):
        prior_x = (prior_entry or {}).get("x_post_id")
        if x_hit.get("post_id") != prior_x:
            return f"Trusted X source (tier {x_hit['author_trust_tier']}): {x_hit['extracted_injury']}"

    rw_status = candidate.get("rotowire_status_raw")
    if rw_status in NEWS_SIGNAL_ROTOWIRE_WORSENING:
        prior_rw = (prior_entry or {}).get("rotowire_status_raw")
        if rw_status != prior_rw:
            return f"RotoWire status worsened to: {rw_status}"

    prior_consensus = (prior_entry or {}).get("prediction_consensus")
    if prior_consensus == "start" and candidate.get("prediction_consensus") == "bench":
        return "Predicted XI moved START -> BENCH"

    return None


def notify_soccer_candidates(candidates, date_str=None):
    """Main entry point - mirrors dnp_alerts.notify_dabble_candidates.
    Tracks all ALERT_THRESHOLDS for calibration, sends Discord only per
    the send rules, fires CRITICAL immediately on confirmed_not_starting,
    and fires SOCCER NEWS SIGNAL on a material new signal even without a
    threshold crossing."""
    from soccer_dates import soccer_today_str
    date_str = date_str or soccer_today_str()
    now_iso = datetime.now(timezone.utc).isoformat()
    data = _load_alerts(date_str)

    seen_keys = set()
    for c in candidates:
        pid = c.get("dabble_player_id")
        fixture_id = c.get("fixture_id")
        if pid is None or fixture_id is None:
            continue
        seen_keys.add((pid, fixture_id))

        # Never send a real-time alert at or after kickoff (item 2,
        # 2026-09-14) - a candidate whose kickoff has already passed (or
        # whose event_date can't even be parsed) is recorded as a data
        # problem instead and skips threshold/critical/news-signal
        # alerting entirely for this fixture. seen_keys is already
        # updated above so the "removed today" bookkeeping below still
        # treats it normally.
        kickoff_status, minutes_to_game = _kickoff_status(c, now_iso)
        if kickoff_status != "upcoming":
            if kickoff_status == "post_kickoff" and c.get("dabble_live", True):
                _record_stale_kickoff_problem(data, c, now_iso, minutes_to_game, "still_live_past_kickoff")
            elif kickoff_status == "unknown":
                _record_stale_kickoff_problem(data, c, now_iso, None, "event_date_unparseable")
            continue

        prior_key = f"_last_state:{pid}:{fixture_id}"
        prior_entry = data.get(prior_key)
        previous_score = (prior_entry or {}).get("dns_score", 0)
        current_score = c["dns_score"]
        data[prior_key] = {
            "dns_score": current_score,
            "rotowire_status_raw": c.get("rotowire_status_raw"),
            "prediction_consensus": c.get("prediction_consensus"),
            "x_post_id": (c.get("x_signal") or {}).get("post_id"),
        }

        crossed = [t for t in ALERT_THRESHOLDS if previous_score < t <= current_score]
        # Only the HIGHEST send-eligible threshold crossed in a single
        # scoring pass triggers an actual Discord send (item 3,
        # 2026-09-14) - found live that a player jumping straight past
        # several send-eligible thresholds in one pass produced one
        # Discord message PER threshold (60 through 90 plus critical, 8
        # messages in the same second for what is really one event).
        # Deliberately computed over DISCORD_SEND_THRESHOLDS, not the
        # raw numeric max of `crossed` - a jump that also blows past a
        # non-sendable threshold (e.g. straight to 95, crossing 90 too)
        # must still send for 85, not suppress entirely because 90 (the
        # true numeric max) isn't itself Discord-eligible. Every
        # threshold still gets its own record/dedup key below -
        # print_speed_report and any other calibration code that reads
        # per-threshold history is unaffected - only the number of
        # actual Discord posts changes.
        sendable_crossed = [t for t in crossed if t in DISCORD_SEND_THRESHOLDS]
        highest_sendable = max(sendable_crossed) if sendable_crossed else None
        for threshold in crossed:
            key = _dedup_key(date_str, pid, fixture_id, threshold)
            if key in data:
                continue
            record = _build_alert_record(c, threshold, date_str, now_iso)
            data[key] = record
            if threshold == highest_sendable and _should_send_discord(c, threshold):
                send_discord_alert(record, _severity_for(threshold))

        if c["official_status"] == "confirmed_not_starting":
            crit_key = _dedup_key(date_str, pid, fixture_id, CRITICAL_KEY)
            if crit_key not in data:
                record = _build_alert_record(c, CRITICAL_KEY, date_str, now_iso)
                data[crit_key] = record
                send_discord_alert(record, SEVERITY_LABELS[CRITICAL_KEY])

        news_reason = _detect_news_signal(c, prior_entry)
        if news_reason:
            news_key = _dedup_key(date_str, pid, fixture_id, f"{NEWS_SIGNAL_KEY}:{hash(news_reason) & 0xffff}")
            if news_key not in data:
                record = _build_alert_record(c, NEWS_SIGNAL_KEY, date_str, now_iso, extra_reason=news_reason)
                data[news_key] = record
                send_discord_alert(record, SEVERITY_LABELS[NEWS_SIGNAL_KEY])

    for key, record in list(data.items()):
        if key.startswith("_last_state:") or key.startswith(STALE_KICKOFF_KEY_PREFIX):
            continue
        pg = (record.get("player_id"), record.get("fixture_id"))
        if pg in seen_keys or pg == (None, None):
            continue
        if record.get("dabble_removed_at") is None:
            record["dabble_removed_at"] = now_iso

    _save_alerts(date_str, data)
    return data


OUTCOMES = ("started", "came_off_bench", "unused_sub", "not_in_squad", "match_postponed", "unknown")
# Did-not-play outcomes for print_speed_report's existing hit-rate math
# (item 4, 2026-09-14) - "came off the bench" is its OWN bucket, never
# folded into a plain "loss," since a bench appearance is a materially
# different real-world outcome from never being in the squad at all.
DID_NOT_PLAY_OUTCOMES = ("came_off_bench", "unused_sub", "not_in_squad")
LONG_TERM_INJURY_DAYS = 14


def grade_alerts(date_str, get_match_squad_fn=None, find_event_fn=None, match_espn_team_id_fn=None):
    """Populate official_started/outcome/graded_at for every alert record
    on date_str whose fixture's official matchday squad has posted -
    mirrors dnp_alerts.grade_alerts's MLB pattern, using ESPN's soccer
    match-squad data as ground truth (the same source soccer_adapter.py's
    own official-lineup enrichment uses).

    2026-09-14 item 4: this was previously a stub that unconditionally
    set official_started=None with no real league/event lookup, AND
    nothing in the pipeline ever called it automatically - grading only
    happened if someone manually ran `python soccer_alerts.py --grade
    <date>`, which nobody had. Both are fixed: this is a real
    implementation, and soccer_dns.py's daily digest now calls it for
    yesterday's date every run (see that module) so grading actually
    accumulates.

    Outcomes (the required split, distinct from a flat True/False):
      started         - named in the matchday squad AND started
      came_off_bench  - named in the squad, active, but did NOT start
      unused_sub      - named in the squad, NOT active at all
      not_in_squad    - the matchday squad posted but this player isn't
                        in it at all (not even on the bench)
      match_postponed - ESPN's own event status says postponed/canceled
                        (a real, positive signal - never inferred merely
                        from "no event found yet")
      unknown         - can't determine yet (event/summary not posted,
                        or the record is missing what grading needs) -
                        stays ungraded, retried on a later run

    official_started (True/False/None) is kept for print_speed_report's
    existing non_start-rate math (True only for "started"; False for
    every DID_NOT_PLAY_OUTCOMES entry); `outcome` carries the full,
    undiluted breakdown. long_term_injury flags a record whose
    Transfermarkt injury predates the match by more than
    LONG_TERM_INJURY_DAYS days - reported separately so an
    already-known injury doesn't inflate the "DNS correctly predicted
    a DNP" record with something that was never really a prediction."""
    if get_match_squad_fn is None:
        from scrapers.espn_soccer import get_match_squad_names as get_match_squad_fn
    if find_event_fn is None:
        from scrapers.espn_soccer import find_event_by_team as find_event_fn
    if match_espn_team_id_fn is None:
        from scrapers.espn_soccer import match_espn_team_id as match_espn_team_id_fn

    data = _load_alerts(date_str)
    if not data:
        print(f"soccer_alerts: no alerts for {date_str} - nothing to grade.")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    event_cache = {}
    squad_cache = {}
    graded = 0
    for key, record in data.items():
        if key.startswith("_last_state:") or key.startswith(STALE_KICKOFF_KEY_PREFIX):
            continue
        if record.get("graded_at") is not None:
            continue  # already settled - never re-grade

        league_code = record.get("league_code")
        team = record.get("team")
        normalized_name = record.get("normalized_name")
        game_start = record.get("game_start")
        if not league_code or not team or not normalized_name or not game_start:
            continue  # missing what grading needs - leave ungraded, never guess

        date_only = game_start[:10]
        event_key = (league_code, team, date_only)
        if event_key not in event_cache:
            team_id = match_espn_team_id_fn(league_code, team)
            event_cache[event_key] = find_event_fn(league_code, team_id, date_only) if team_id else None
        event = event_cache[event_key]
        if event is None:
            continue  # not posted yet (or unresolvable) - try again on a later grading run

        status_name = ((event.get("status") or {}).get("type") or {}).get("name", "")
        if "POSTPON" in status_name.upper() or "CANCEL" in status_name.upper():
            record["outcome"] = "match_postponed"
            record["official_started"] = None
            record["graded_at"] = now_iso
            graded += 1
            continue

        event_id = event["id"]
        if event_id not in squad_cache:
            squad_cache[event_id] = get_match_squad_fn(league_code, event_id)
        squad = squad_cache[event_id]
        if not squad:
            continue  # match summary not posted yet - try again on a later grading run

        info = squad.get(normalized_name)
        if info is None:
            outcome, started = "not_in_squad", False
        elif info["starter"]:
            outcome, started = "started", True
        elif info["active"]:
            outcome, started = "came_off_bench", False
        else:
            outcome, started = "unused_sub", False

        long_term_injury = False
        since = record.get("transfermarkt_injury_since")
        if since:
            try:
                since_dt = datetime.strptime(since[:10], "%Y-%m-%d")
                match_dt = datetime.strptime(date_only, "%Y-%m-%d")
                long_term_injury = (match_dt - since_dt).days > LONG_TERM_INJURY_DAYS
            except ValueError:
                pass

        record["outcome"] = outcome
        record["official_started"] = started
        record["long_term_injury"] = long_term_injury
        record["graded_at"] = now_iso
        graded += 1

    _save_alerts(date_str, data)
    print(f"soccer_alerts: graded {graded} alert record(s) for {date_str}.")


def _all_alert_records():
    records = []
    if not os.path.isdir(ALERTS_DIR):
        return records
    for fname in sorted(os.listdir(ALERTS_DIR)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(ALERTS_DIR, fname), encoding="utf-8") as f:
            data = json.load(f)
        for key, record in data.items():
            if key.startswith("_last_state:") or key.startswith(STALE_KICKOFF_KEY_PREFIX):
                continue
            records.append(record)
    return records


def print_speed_report():
    """python soccer_alerts.py --speed-report - item 7: which source
    (trusted X reporter / RotoWire / predicted lineup / Dabble pull) gave
    the most lead time, and how accurate each was. Honest about small
    samples - see build_report's per-source n."""
    records = _all_alert_records()
    by_source = {}
    for r in records:
        source = "x_trusted" if (r.get("extra_reason") or "").startswith("Trusted X") else \
                 "rotowire" if (r.get("extra_reason") or "").startswith("RotoWire") else \
                 "predicted_xi" if (r.get("extra_reason") or "").startswith("Predicted XI") else \
                 "dabble_pull" if r["alert_type"] == CRITICAL_KEY else "threshold_cross"
        by_source.setdefault(source, []).append(r)

    print(f"{'Source':<16}{'Signals':<10}{'MedLeadGameMin':<16}{'MedLeadRemovalMin':<18}Accuracy")
    for source, recs in sorted(by_source.items()):
        n = len(recs)
        leads = [r["minutes_to_game"] for r in recs if r.get("minutes_to_game") is not None]
        med_lead = round(statistics.median(leads), 1) if leads else None
        graded = [r for r in recs if r.get("official_started") is not None]
        non_start = sum(1 for r in graded if r["official_started"] is False)
        accuracy = round(100 * non_start / len(graded), 1) if graded else None
        print(f"{source:<16}{n:<10}{(str(med_lead) + ' min') if med_lead is not None else 'n/a':<16}"
              f"{'n/a':<18}{(str(accuracy) + '%') if accuracy is not None else 'n/a'}")
    print("\nOrdering (trusted X reporter > RotoWire > predicted lineup > Dabble pull) is UNPROVEN until "
          "each row above has real, graded volume - do not read a ranking into small-n rows.")


def main():
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Soccer DNS Discord alerts + source-speed report")
    parser.add_argument("--speed-report", action="store_true")
    parser.add_argument("--grade", default=None, help="date YYYY-MM-DD")
    args = parser.parse_args()
    if args.speed_report:
        print_speed_report()
    elif args.grade:
        grade_alerts(args.grade)
    else:
        parser.error("specify --speed-report or --grade")


if __name__ == "__main__":
    main()
