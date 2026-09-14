"""
Standalone Soccer DNS command-center page (docs/soccer-dns.html) - split
out of report.py's MLB dashboard entirely (2026-09-14: MLB DNP/DNS
discontinued, Soccer DNS gets its own dedicated page rather than living
as a tab inside the MLB fantasy dashboard). No scoring logic here -
display only, straight off soccer_adapter.py's scored candidates plus
the persisted live snapshot / alert history / digest status files.

Sections (per the 2026-09-14 scope-change request):
  - Top status bar: live/WATCH/ALERT/HIGH/CRITICAL/REMOVED counts, Discord
    + X status, last refresh time.
  - LIVE candidates table.
  - REMOVED (Dabble pulled the line) - first seen / first alert / removed
    at / lead time, so this is where "how much lead time did we actually
    get" lives.
  - ALERT HISTORY - every real-time alert soccer_alerts.py has fired.
  - SOURCE HEALTH - Dabble/RotoWire/Predicted XI/Start History/X/Discord,
    each with enabled/disabled + coverage/last-update.
"""

import json
import os
from datetime import datetime, timezone

WATCH_THRESHOLD = 70
ALERT_THRESHOLD = 75
HIGH_THRESHOLD = 85

LIVE_SNAPSHOTS_DIR = os.path.join("data", "soccer_dnp_live_snapshots")
ALERTS_DIR = os.path.join("data", "soccer_dns_alerts")
DIGEST_DIR = os.path.join("data", "soccer_daily_digest")


def _game_time_pt(event_date):
    try:
        from report import game_time_pt
        return game_time_pt(event_date)
    except Exception:
        return event_date or "TBD"


def _load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _removed_candidates(date_str, alerts_by_player_fixture):
    """[{name, team, firstSeen, firstAlert, removedAt, leadTimeMinutes}]
    for every entry in today's live snapshot whose last score_history
    timestamp is NOT this run's _latest_batch_ts - i.e. Dabble no longer
    has a live prop for them. leadTimeMinutes is firstAlert -> removedAt
    (how much warning a real-time alert gave before the line vanished),
    None if no alert ever fired for that player."""
    data = _load_json(os.path.join(LIVE_SNAPSHOTS_DIR, f"{date_str}.json"))
    if not data:
        return []
    batch_ts = data.get("_latest_batch_ts")
    removed = []
    for key, entry in data.items():
        if key.startswith("_"):
            continue
        history = entry.get("score_history") or []
        if not history or history[-1].get("ts") == batch_ts:
            continue
        first_seen = history[0].get("ts")
        removed_at = history[-1].get("ts")
        pid = entry.get("dabble_player_id")
        fixture_id = entry.get("fixture_id")
        first_alert = alerts_by_player_fixture.get((pid, fixture_id))
        lead_minutes = None
        if first_alert and removed_at:
            try:
                a = datetime.fromisoformat(first_alert.replace("Z", "+00:00"))
                r = datetime.fromisoformat(removed_at.replace("Z", "+00:00"))
                lead_minutes = round((r - a).total_seconds() / 60, 1)
            except Exception:
                lead_minutes = None
        removed.append({
            "name": entry.get("name"), "team": entry.get("team"),
            "firstSeen": first_seen, "firstAlert": first_alert,
            "removedAt": removed_at, "leadTimeMinutes": lead_minutes,
            "peakDnsScore": max((h.get("dns_score", 0) for h in history), default=0),
        })
    removed.sort(key=lambda r: r["removedAt"] or "", reverse=True)
    return removed


def _alert_history(date_str):
    data = _load_json(os.path.join(ALERTS_DIR, f"{date_str}.json")) or {}
    records = [v for k, v in data.items() if not k.startswith("_last_state:")]
    records.sort(key=lambda r: r.get("alerted_at") or "", reverse=True)
    by_player_fixture = {}
    for r in sorted(records, key=lambda r: r.get("alerted_at") or ""):
        pid, fx = r.get("player_id"), r.get("fixture_id")
        if (pid, fx) not in by_player_fixture:
            by_player_fixture[(pid, fx)] = r.get("alerted_at")
    return records, by_player_fixture


def _source_health(candidates):
    """Enabled/disabled + coverage/last-update per source, aggregated
    straight off each candidate's own source_health triple (see
    soccer_adapter.enrich_and_score_player) - no re-derivation."""
    n = len(candidates) or 1
    rotowire_matched = sum(1 for c in candidates if c["source_health"]["rotowire"]["entity_matched"])
    transfermarkt_matched = sum(1 for c in candidates if c["source_health"]["transfermarkt"]["entity_matched"])
    official_matched = sum(1 for c in candidates if c["source_health"]["official_lineup"]["entity_matched"])
    history_matched = sum(1 for c in candidates if c["has_history"])
    predicted_xi_matched = sum(1 for c in candidates if c["has_predicted_xi"])
    x_attempted = any(c["source_health"]["x"]["source_attempted"] for c in candidates)

    try:
        import soccer_x_monitor
        x_enabled = soccer_x_monitor.has_credentials()
    except Exception:
        x_enabled = False

    discord_configured = bool(os.environ.get("DISCORD_SOCCER_DNS_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL"))

    return {
        "dabble": {"enabled": True, "coverage": f"{len(candidates)}/{len(candidates)} (100%)"},
        "rotowire": {"enabled": True, "coverage": f"{rotowire_matched}/{len(candidates)} ({round(100*rotowire_matched/n)}%)"},
        "predictedXi": {"enabled": True, "coverage": f"{predicted_xi_matched}/{len(candidates)} ({round(100*predicted_xi_matched/n)}%)"},
        "startHistory": {"enabled": True, "coverage": f"{history_matched}/{len(candidates)} ({round(100*history_matched/n)}%)"},
        "transfermarkt": {"enabled": True, "coverage": f"{transfermarkt_matched}/{len(candidates)} ({round(100*transfermarkt_matched/n)}%)"},
        "officialLineup": {"enabled": True, "coverage": f"{official_matched}/{len(candidates)} ({round(100*official_matched/n)}%)"},
        "x": {"enabled": x_enabled, "coverage": "n/a" if not x_enabled else "live"},
        "discord": {"enabled": discord_configured, "coverage": "configured" if discord_configured else "NOT CONFIGURED"},
    }


def _status_counts(candidates):
    live = len(candidates)
    watch = sum(1 for c in candidates if WATCH_THRESHOLD <= c["dns_score"] < ALERT_THRESHOLD)
    alert = sum(1 for c in candidates if ALERT_THRESHOLD <= c["dns_score"] < HIGH_THRESHOLD)
    high = sum(1 for c in candidates if c["dns_score"] >= HIGH_THRESHOLD)
    critical = sum(1 for c in candidates if c["official_status"] == "confirmed_not_starting")
    return live, watch, alert, high, critical


def _candidate_row(c):
    dabble = c.get("dabble_status_normalized") or c.get("dabble_status_raw") or "LIVE"
    rw = c.get("rotowire_page_tag") or c.get("rotowire_status_raw") or "-"
    xi = c.get("prediction_consensus", "unknown")
    news = (c.get("transfermarkt_injury") or {}).get("reason") or (c.get("rotowire_injury") if c.get("has_news") else None) or "-"
    reasons = "; ".join(c.get("top_reasons", [])[:3])
    return {
        "dns": c["dns_score"], "conf": c["confidence_score"], "urg": c["urgency_score"],
        "pri": c.get("combined_priority"), "name": c["player_name"], "team": c["team"],
        "matchup": c.get("matchup") or c["team"], "league": c.get("league_code") or c.get("league_raw") or "?",
        "kickoff": _game_time_pt(c.get("event_date")), "dabble": dabble, "rotowire": rw,
        "predictedXi": xi, "news": news, "reasons": reasons, "officialStatus": c.get("official_status"),
    }


def generate(candidates, date_str=None, out_path=os.path.join("docs", "soccer-dns.html")):
    date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc)

    candidates = sorted(candidates, key=lambda c: c.get("combined_priority") or 0, reverse=True)
    live, watch, alert, high, critical = _status_counts(candidates)
    alert_records, alerts_by_player_fixture = _alert_history(date_str)
    removed = _removed_candidates(date_str, alerts_by_player_fixture)
    source_health = _source_health(candidates)
    digest_record = _load_json(os.path.join(DIGEST_DIR, f"{date_str}.json"))

    rows_js = json.dumps([_candidate_row(c) for c in candidates], ensure_ascii=False)
    removed_js = json.dumps(removed, ensure_ascii=False)
    alerts_js = json.dumps(alert_records, ensure_ascii=False)
    health_js = json.dumps(source_health, ensure_ascii=False)
    digest_js = json.dumps(digest_record, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Soccer DNS — Command Center</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 0; background: #0d1626; color: #e6e9f0; }}
  header {{ background: #0a1120; padding: 16px 24px; border-bottom: 1px solid #1c2944; }}
  header h1 {{ margin: 0 0 4px 0; font-size: 20px; }}
  .subnav a {{ color: #9fb0cc; text-decoration: none; margin-right: 16px; font-size: 14px; }}
  .subnav a.active {{ color: #fff; font-weight: 700; }}
  .subnav a:hover {{ color: #fff; }}
  main {{ padding: 20px 24px 60px; max-width: 1400px; margin: 0 auto; }}

  .status-bar {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }}
  .status-chip {{ background: #16213a; border: 1px solid #2a3a5c; border-radius: 8px; padding: 10px 16px; min-width: 110px; }}
  .status-chip .value {{ font-size: 24px; font-weight: 900; }}
  .status-chip .label {{ font-size: 11px; color: #9fb0cc; text-transform: uppercase; letter-spacing: 0.5px; }}
  .status-chip.watch .value {{ color: #f1c40f; }}
  .status-chip.alert .value {{ color: #e67e22; }}
  .status-chip.high .value {{ color: #e74c3c; }}
  .status-chip.critical .value {{ color: #8e44ad; }}
  .status-chip.ok .value {{ color: #2ecc71; }}
  .status-chip.bad .value {{ color: #e74c3c; }}

  h2.section-title {{ font-size: 16px; margin: 32px 0 12px; border-bottom: 1px solid #1c2944; padding-bottom: 6px; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #1c2944; white-space: nowrap; }}
  th {{ color: #9fb0cc; font-weight: 700; cursor: pointer; position: sticky; top: 0; background: #0d1626; }}
  td.reasons, td.news {{ white-space: normal; max-width: 320px; }}
  tr:hover td {{ background: #16213a; }}
  .tier-high {{ color: #e74c3c; font-weight: 800; }}
  .tier-alert {{ color: #e67e22; font-weight: 800; }}
  .tier-watch {{ color: #f1c40f; font-weight: 800; }}

  .health-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }}
  .health-card {{ background: #16213a; border: 1px solid #2a3a5c; border-radius: 8px; padding: 12px 14px; }}
  .health-card .name {{ font-weight: 700; margin-bottom: 4px; }}
  .health-card .state {{ font-size: 12px; }}
  .health-card .state.on {{ color: #2ecc71; }}
  .health-card .state.off {{ color: #e74c3c; }}
  .empty-msg {{ color: #6b7a99; padding: 16px 0; }}
  .table-wrap {{ overflow-x: auto; }}
</style>
</head>
<body>
<header>
  <h1>⚽ Soccer DNS — Command Center</h1>
  <div class="subnav"><a href="index.html">MLB Dashboard</a><a href="soccer-dns.html" class="active">Soccer DNS</a></div>
</header>
<main>

  <div class="status-bar">
    <div class="status-chip"><div class="value" id="chipLive">{live}</div><div class="label">Dabble Live</div></div>
    <div class="status-chip watch"><div class="value" id="chipWatch">{watch}</div><div class="label">Watch (70+)</div></div>
    <div class="status-chip alert"><div class="value" id="chipAlert">{alert}</div><div class="label">Alert (75+)</div></div>
    <div class="status-chip high"><div class="value" id="chipHigh">{high}</div><div class="label">High (85+)</div></div>
    <div class="status-chip critical"><div class="value" id="chipCritical">{critical}</div><div class="label">Critical</div></div>
    <div class="status-chip"><div class="value" id="chipRemoved">{len(removed)}</div><div class="label">Removed Today</div></div>
    <div class="status-chip {'ok' if source_health['discord']['enabled'] else 'bad'}"><div class="value">{'ON' if source_health['discord']['enabled'] else 'OFF'}</div><div class="label">Discord</div></div>
    <div class="status-chip {'ok' if source_health['x']['enabled'] else 'bad'}"><div class="value">{'ON' if source_health['x']['enabled'] else 'OFF'}</div><div class="label">X Realtime</div></div>
    <div class="status-chip"><div class="value" style="font-size:14px">{now.strftime('%I:%M %p UTC')}</div><div class="label">Last Refresh</div></div>
  </div>

  <h2 class="section-title">Live Candidates</h2>
  <div class="table-wrap">
  <table id="liveTable">
    <thead><tr>
      <th>DNS</th><th>Confidence</th><th>Urgency</th><th>Priority</th><th>Player</th><th>Match</th>
      <th>League</th><th>Kickoff</th><th>Dabble</th><th>RotoWire</th><th>Predicted XI</th><th>News</th><th>Reasons</th>
    </tr></thead>
    <tbody id="liveBody"></tbody>
  </table>
  </div>

  <h2 class="section-title">Removed (Dabble line pulled)</h2>
  <div class="table-wrap">
  <table id="removedTable">
    <thead><tr><th>Player</th><th>Team</th><th>First Seen</th><th>First Alert</th><th>Removed At</th><th>Lead Time (min)</th><th>Peak DNS</th></tr></thead>
    <tbody id="removedBody"></tbody>
  </table>
  </div>

  <h2 class="section-title">Alert History (today)</h2>
  <div class="table-wrap">
  <table id="alertsTable">
    <thead><tr><th>Time</th><th>Type</th><th>Player</th><th>Team</th><th>DNS</th><th>Confidence</th><th>Note</th></tr></thead>
    <tbody id="alertsBody"></tbody>
  </table>
  </div>

  <h2 class="section-title">Source Health</h2>
  <div class="health-grid" id="healthGrid"></div>

</main>
<script>
const LIVE = {rows_js};
const REMOVED = {removed_js};
const ALERTS = {alerts_js};
const HEALTH = {health_js};
const DIGEST = {digest_js};

function tierClass(dns) {{
  if (dns >= 85) return 'tier-high';
  if (dns >= 75) return 'tier-alert';
  if (dns >= 70) return 'tier-watch';
  return '';
}}

const liveBody = document.getElementById('liveBody');
if (LIVE.length === 0) {{
  liveBody.innerHTML = '<tr><td colspan="13" class="empty-msg">No live Soccer DNS candidates.</td></tr>';
}} else {{
  for (const c of LIVE) {{
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="${{tierClass(c.dns)}}">${{c.dns}}</td><td>${{c.conf}}</td><td>${{c.urg}}</td><td>${{c.pri}}</td>
      <td>${{c.name}}</td><td>${{c.matchup}}</td><td>${{c.league}}</td><td>${{c.kickoff}}</td>
      <td>${{c.dabble}}</td><td>${{c.rotowire}}</td><td>${{c.predictedXi}}</td>
      <td class="news">${{c.news}}</td><td class="reasons">${{c.reasons}}</td>`;
    liveBody.appendChild(tr);
  }}
}}

const removedBody = document.getElementById('removedBody');
if (REMOVED.length === 0) {{
  removedBody.innerHTML = '<tr><td colspan="7" class="empty-msg">No candidates removed from the board today.</td></tr>';
}} else {{
  for (const r of REMOVED) {{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${{r.name}}</td><td>${{r.team}}</td><td>${{r.firstSeen || ''}}</td>
      <td>${{r.firstAlert || 'none'}}</td><td>${{r.removedAt || ''}}</td>
      <td>${{r.leadTimeMinutes != null ? r.leadTimeMinutes : 'n/a'}}</td><td>${{r.peakDnsScore}}</td>`;
    removedBody.appendChild(tr);
  }}
}}

const alertsBody = document.getElementById('alertsBody');
if (ALERTS.length === 0) {{
  alertsBody.innerHTML = '<tr><td colspan="7" class="empty-msg">No alerts fired today.</td></tr>';
}} else {{
  for (const a of ALERTS) {{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${{a.alerted_at || ''}}</td><td>${{a.alert_type}}</td><td>${{a.player_name}}</td>
      <td>${{a.team}}</td><td>${{a.dns_score}}</td><td>${{a.confidence_score}}</td>
      <td>${{a.extra_reason || (a.top_reasons || []).slice(0,2).join('; ')}}</td>`;
    alertsBody.appendChild(tr);
  }}
}}

const healthGrid = document.getElementById('healthGrid');
const HEALTH_LABELS = {{
  dabble: 'Dabble', rotowire: 'RotoWire', predictedXi: 'Predicted XI', startHistory: 'Start History',
  transfermarkt: 'Transfermarkt', officialLineup: 'Official Lineup', x: 'X (Twitter)', discord: 'Discord',
}};
for (const key of Object.keys(HEALTH_LABELS)) {{
  const h = HEALTH[key];
  const card = document.createElement('div');
  card.className = 'health-card';
  card.innerHTML = `<div class="name">${{HEALTH_LABELS[key]}}</div>
    <div class="state ${{h.enabled ? 'on' : 'off'}}">${{h.enabled ? 'ENABLED' : 'DISABLED'}}</div>
    <div class="state">${{h.coverage}}</div>`;
  healthGrid.appendChild(card);
}}
if (DIGEST) {{
  const card = document.createElement('div');
  card.className = 'health-card';
  card.innerHTML = `<div class="name">Daily Digest</div>
    <div class="state ${{DIGEST.discord_delivered ? 'on' : 'off'}}">${{DIGEST.discord_delivered ? 'DELIVERED' : 'NOT DELIVERED'}}</div>
    <div class="state">${{DIGEST.candidate_count}} candidates &middot; ${{DIGEST.generated_at || ''}}</div>`;
  healthGrid.appendChild(card);
}}
</script>
</body>
</html>
"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path
