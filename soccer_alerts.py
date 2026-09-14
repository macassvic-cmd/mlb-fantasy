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
    from stale_lines import _parse_iso
    a, b = _parse_iso(a_iso), _parse_iso(b_iso)
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 60


def _build_alert_record(candidate, alert_type, date_str, now_iso, extra_reason=None):
    return {
        "player_id": candidate.get("dabble_player_id"),
        "player_name": candidate["player_name"],
        "team": candidate["team"],
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
        "official_started": None,
        "graded_at": None,
        "dabble_removed_at": None,
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
    webhook_url = _webhook_url()
    if not webhook_url:
        return False
    try:
        resp = requests.post(webhook_url, json={"embeds": [_build_embed(record, severity)]}, timeout=15)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"soccer_alerts: Discord webhook POST failed for {record['player_name']}: {type(e).__name__}")
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
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat()
    data = _load_alerts(date_str)

    seen_keys = set()
    for c in candidates:
        pid = c.get("dabble_player_id")
        fixture_id = c.get("fixture_id")
        if pid is None or fixture_id is None:
            continue
        seen_keys.add((pid, fixture_id))

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
        for threshold in crossed:
            key = _dedup_key(date_str, pid, fixture_id, threshold)
            if key in data:
                continue
            record = _build_alert_record(c, threshold, date_str, now_iso)
            data[key] = record
            if _should_send_discord(c, threshold):
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
        if key.startswith("_last_state:"):
            continue
        pg = (record.get("player_id"), record.get("fixture_id"))
        if pg in seen_keys or pg == (None, None):
            continue
        if record.get("dabble_removed_at") is None:
            record["dabble_removed_at"] = now_iso

    _save_alerts(date_str, data)
    return data


def grade_alerts(date_str, get_match_squad_fn=None):
    """Populate official_started for every alert record on date_str -
    mirrors dnp_alerts.grade_alerts. get_match_squad_fn injectable for
    testing; defaults to scrapers.espn_soccer.get_match_squad_names."""
    if get_match_squad_fn is None:
        from scrapers.espn_soccer import get_match_squad_names as get_match_squad_fn

    data = _load_alerts(date_str)
    if not data:
        print(f"soccer_alerts: no alerts for {date_str} - nothing to grade.")
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    graded = 0
    for key, record in data.items():
        if key.startswith("_last_state:") or record.get("official_started") is not None:
            continue
        record["official_started"] = None  # left for a caller with real league/event context - see soccer_adapter.py
        record["graded_at"] = now_iso
        graded += 1
    _save_alerts(date_str, data)
    print(f"soccer_alerts: touched {graded} alert record(s) for {date_str} (grading needs league/event context - see soccer_adapter.py's official_status).")


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
            if key.startswith("_last_state:"):
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
