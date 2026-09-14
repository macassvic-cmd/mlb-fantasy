"""
Discord notifications + threshold-performance tracking for the MLB DNP
scorer. Reads already-scored Dabble candidates (dabble_adapter.py) - never
recomputes or duplicates scoring logic here.

The webhook URL comes ONLY from the DISCORD_DNP_WEBHOOK_URL environment
variable - never hardcoded, never logged, never persisted into any
snapshot/alert record. A missing/invalid webhook makes every send a silent
no-op (same non-fatal pattern as stale_lines.py's existing Discord
integration) - a Discord outage must never break scoring or grading.

Alert rules (score = dnp_score, all live-board candidates by construction
already have a live Dabble prop - see module docstring on "dabble_live"):
  score < 70                                          -> no alert
  70-74                                                -> WATCH (tracked only, no Discord)
  score >= 75 and confidence >= 50                     -> DNP ALERT
  score >= 85 and confidence >= 60                     -> HIGH PRIORITY
  projected_start_status == confirmed_not_starting     -> CONFIRMED NON-STARTER (immediate, any score)

Every threshold in ALERT_THRESHOLDS (60/65/70/75/80/85/90 - the same
buckets --report uses) is tracked/persisted for calibration purposes even
when the tier doesn't actually send to Discord (60/65 exist purely so
--report has a full calibration curve, matching dnp_score.py's coarser
bucket table at finer granularity). Only 75/85/confirmed-not-starting ever
POST to Discord.

Never call the score a probability in Discord copy - label it "DNP Score"
until calibration data (see --report) demonstrates real bucket accuracy.

Usage:
  python dnp_alerts.py --report              # calibration/lead-time report
  python dnp_alerts.py --grade 2026-09-10    # populate official_started for a date
"""

import argparse
import json
import os
import statistics
from datetime import datetime, timezone

import requests

from scrapers.mlb_api import get_game_start_data
from stale_lines import _parse_iso

ALERTS_DIR = os.path.join("data", "dnp_alerts")
WEBHOOK_ENV_VAR = "DISCORD_DNP_WEBHOOK_URL"

ALERT_THRESHOLDS = [60, 65, 70, 75, 80, 85, 90]  # tracked/persisted for --report's calibration curve
DISCORD_SEND_THRESHOLDS = {75, 85}               # only these (+ CRITICAL) actually POST to Discord
CRITICAL_KEY = "critical"

ALERT_MIN_CONFIDENCE = {75: 50, 85: 60}

SEVERITY_LABELS = {70: "WATCH", 75: "DNP ALERT", 85: "HIGH PRIORITY", CRITICAL_KEY: "CONFIRMED NON-STARTER"}
SEVERITY_COLORS = {"WATCH": 0x95A5A6, "DNP ALERT": 0xE67E22, "HIGH PRIORITY": 0xE74C3C,
                    "CONFIRMED NON-STARTER": 0x8E44AD}


def _webhook_url():
    return os.environ.get(WEBHOOK_ENV_VAR)


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
        json.dump(data, f, indent=2)


def _dedup_key(date_str, player_id, game_id, threshold):
    return f"{date_str}:{player_id}:{game_id}:{threshold}"


def _minutes_between(a_iso, b_iso):
    a, b = _parse_iso(a_iso), _parse_iso(b_iso)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60


def _format_markets(props):
    return ", ".join(f"{p['market']} {p['line']}" for p in props) or "(none live)"


def _build_alert_record(candidate, threshold, date_str, now_iso):
    game_start = candidate.get("game_start")
    return {
        "player_id": candidate.get("player_id"),
        "player_name": candidate["player_name"],
        "team": candidate["team"],
        "opponent": candidate.get("opponent"),
        "game_id": candidate.get("game_id"),
        "dnp_score": candidate["dnp_score"],
        "confidence_score": candidate["confidence_score"],
        "urgency_score": candidate["urgency_score"],
        "threshold": threshold,
        "alerted_at": now_iso,
        "game_start": game_start,
        "minutes_to_game": _minutes_between(now_iso, game_start),
        "projected_start_status": candidate["projected_start_status"],
        "dabble_live": True,  # always true at creation time - a candidate only exists here because
                               # dabble_adapter just saw a live prop for them this very pass
        "dabble_markets": [p["market"] for p in candidate.get("props", [])],
        "dabble_prop_ids": [p["prop_id"] for p in candidate.get("props", [])],
        "top_reasons": candidate.get("top_reasons", []),
        "official_started": None,
        "graded_at": None,
        "dabble_removed_at": None,
        "minutes_alert_to_dabble_removal": None,
    }


def _severity_for(threshold):
    return SEVERITY_LABELS.get(threshold, "WATCH")


def _should_send_discord(candidate, threshold):
    if candidate["projected_start_status"] == "confirmed_not_starting":
        return True  # CRITICAL overrides everything else
    if threshold not in DISCORD_SEND_THRESHOLDS:
        return False
    return candidate["confidence_score"] >= ALERT_MIN_CONFIDENCE[threshold]


def _build_embed(record, severity):
    game_time = record.get("game_start") or "unknown"
    color = SEVERITY_COLORS.get(severity, 0x95A5A6)
    matchup = f"{record['team']} vs {record['opponent']}" if record.get("opponent") else record["team"]
    return {
        "title": f"{severity} - {record['player_name']}",
        "description": f"{matchup} - {game_time}",
        "color": color,
        "fields": [
            {"name": "DNP Score", "value": str(record["dnp_score"]), "inline": True},
            {"name": "Confidence", "value": str(record["confidence_score"]), "inline": True},
            {"name": "Urgency", "value": str(record["urgency_score"]), "inline": True},
            {"name": "Lineup status", "value": record["projected_start_status"], "inline": True},
            {"name": "Dabble", "value": "LIVE" if record["dabble_live"] else "REMOVED", "inline": True},
            {"name": "Live markets", "value": ", ".join(record["dabble_markets"]) or "(none)", "inline": False},
            {"name": "Top reasons", "value": "\n".join(record["top_reasons"][:5]) or "(none)", "inline": False},
        ],
        "footer": {"text": "DNP Score is a heuristic ranking, not a calibrated probability yet - see dnp_alerts.py --report"},
    }


def send_discord_alert(record, severity):
    """POST one alert embed. No-op (returns False) if DISCORD_DNP_WEBHOOK_URL
    isn't set or the POST fails for any reason - never raises, never logs
    the webhook URL itself."""
    webhook_url = _webhook_url()
    if not webhook_url:
        return False
    try:
        resp = requests.post(webhook_url, json={"embeds": [_build_embed(record, severity)]}, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"dnp_alerts: Discord webhook POST failed for {record['player_name']}: {type(e).__name__}")
        return False


def notify_dabble_candidates(candidates, date_str=None):
    """Main entry point - receives dabble_adapter.score_dabble_board()'s
    output. For each candidate: determines which ALERT_THRESHOLDS were
    newly crossed since the last time we saw this exact (player, game)
    today (dedup key date+player_id+game_id+threshold - a threshold only
    fires once per player per game per day, and only on an upward
    crossing, never on every refresh), persists a record for each newly-
    crossed threshold, and sends to Discord per the send rules. Also
    detects candidates that dropped OFF the board since the last call
    (Dabble removed their last live prop) and stamps dabble_removed_at on
    any of their still-open alert records."""
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat()
    data = _load_alerts(date_str)

    seen_player_game_keys = set()
    for c in candidates:
        pid, game_id = c.get("player_id"), c.get("game_id")
        if pid is None or game_id is None:
            continue  # can't dedup/track without a stable identity
        seen_player_game_keys.add((pid, game_id))

        prev_key = f"_last_score:{pid}:{game_id}"
        previous_score = data.get(prev_key, 0)
        current_score = c["dnp_score"]
        data[prev_key] = current_score  # bookkeeping only, not an alert record - skipped by report/grading

        crossed = [t for t in ALERT_THRESHOLDS if previous_score < t <= current_score]
        for threshold in crossed:
            key = _dedup_key(date_str, pid, game_id, threshold)
            if key in data:
                continue  # already alerted this threshold for this player/game today
            record = _build_alert_record(c, threshold, date_str, now_iso)
            data[key] = record
            if _should_send_discord(c, threshold):
                send_discord_alert(record, _severity_for(threshold))

        if c["projected_start_status"] == "confirmed_not_starting":
            crit_key = _dedup_key(date_str, pid, game_id, CRITICAL_KEY)
            if crit_key not in data:
                record = _build_alert_record(c, CRITICAL_KEY, date_str, now_iso)
                data[crit_key] = record
                send_discord_alert(record, SEVERITY_LABELS[CRITICAL_KEY])

    # Dabble removal detection: any tracked alert record for a (player,
    # game) no longer present in this pass's candidates, not yet marked
    # removed, gets stamped now.
    for key, record in data.items():
        if key.startswith("_last_score:"):
            continue
        pg = (record.get("player_id"), record.get("game_id"))
        if pg in seen_player_game_keys or pg == (None, None):
            continue
        if record.get("dabble_removed_at") is None:
            record["dabble_removed_at"] = now_iso
            lead = _minutes_between(record["alerted_at"], now_iso)
            record["minutes_alert_to_dabble_removal"] = lead

    _save_alerts(date_str, data)
    return data


def grade_alerts(date_str):
    """Populate official_started/graded_at for every alert record on
    date_str, once games are Final - same get_game_start_data source
    dnp_score.grade_dnp_scores and start_history.py already use."""
    data = _load_alerts(date_str)
    if not data:
        print(f"dnp_alerts: no alerts for {date_str} - nothing to grade.")
        return

    game_cache = {}
    now_iso = datetime.now(timezone.utc).isoformat()
    graded_count = 0
    for key, record in data.items():
        if key.startswith("_last_score:") or record.get("official_started") is not None:
            continue
        game_id, pid = record.get("game_id"), record.get("player_id")
        if game_id is None or pid is None:
            continue
        if game_id not in game_cache:
            game_cache[game_id] = get_game_start_data(game_id)
        game_data = game_cache[game_id]
        if not game_data:
            continue
        started = None
        for side in ("home", "away"):
            for p in game_data[side]["players"]:
                if p.get("player_id") == pid:
                    started = p["started"]
                    break
            if started is not None:
                break
        if started is None:
            continue
        record["official_started"] = started
        record["graded_at"] = now_iso
        graded_count += 1

    _save_alerts(date_str, data)
    print(f"dnp_alerts: graded {graded_count} alert record(s) for {date_str}.")


# ---------------------------------------------------------------------------
# Calibration / lead-time report
# ---------------------------------------------------------------------------

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
            if key.startswith("_last_score:"):
                continue
            records.append(record)
    return records


def build_report(thresholds=ALERT_THRESHOLDS):
    records = _all_alert_records()
    rows = []
    for t in thresholds:
        bucket = [r for r in records if r["threshold"] == t]
        n = len(bucket)
        graded = [r for r in bucket if r.get("official_started") is not None]
        non_start = sum(1 for r in graded if r["official_started"] is False)
        hit_rate = round(100 * non_start / len(graded), 1) if graded else None
        false_positive_rate = round(100 * (len(graded) - non_start) / len(graded), 1) if graded else None

        lead_times = [r["minutes_to_game"] for r in bucket if r.get("minutes_to_game") is not None]
        median_lead = round(statistics.median(lead_times), 1) if lead_times else None

        removal_leads = [r["minutes_alert_to_dabble_removal"] for r in bucket
                          if r.get("minutes_alert_to_dabble_removal") is not None]
        median_removal_lead = round(statistics.median(removal_leads), 1) if removal_leads else None

        pct_live_at_alert = round(100 * sum(1 for r in bucket if r["dabble_live"]) / n, 1) if n else None

        rows.append({
            "threshold": t, "n": n, "graded_n": len(graded), "non_start_count": non_start,
            "hit_rate": hit_rate, "false_positive_rate": false_positive_rate,
            "median_lead_minutes": median_lead, "median_removal_lead_minutes": median_removal_lead,
            "pct_dabble_live_at_alert": pct_live_at_alert,
        })
    return rows


def print_report():
    rows = build_report()
    print(f"{'>=Score':<9}{'n':<6}{'Graded':<8}{'NonStart':<10}{'HitRate':<10}{'FalsePos':<10}"
          f"{'MedLeadMin':<12}{'MedRemovalMin':<15}{'%LiveAtAlert'}")
    for r in rows:
        def fmt(v, suffix="%"):
            return f"{v}{suffix}" if v is not None else "n/a"
        print(f">={r['threshold']:<7}{r['n']:<6}{r['graded_n']:<8}{r['non_start_count']:<10}"
              f"{fmt(r['hit_rate']):<10}{fmt(r['false_positive_rate']):<10}"
              f"{fmt(r['median_lead_minutes'], ' min'):<12}{fmt(r['median_removal_lead_minutes'], ' min'):<15}"
              f"{fmt(r['pct_dabble_live_at_alert'])}")


def main():
    parser = argparse.ArgumentParser(description="MLB DNP Discord alerts + calibration report")
    parser.add_argument("--report", action="store_true", help="print the threshold calibration/lead-time report")
    parser.add_argument("--grade", default=None, help="populate official_started for a date YYYY-MM-DD")
    args = parser.parse_args()

    if args.report:
        print_report()
    elif args.grade:
        grade_alerts(args.grade)
    else:
        parser.error("specify --report or --grade")


if __name__ == "__main__":
    main()
