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


def _coverage_matrix(candidates):
    """[{name, team, history, injury, news, predictedXi, x, sourceCount,
    consensus}] - one row per live player, one column per raw source -
    item 5's "exactly which sources every player has" (2026-09-14).
    Booleans straight off each candidate's own has_*/source_health -
    no re-derivation, so this can never drift from the coverage_report
    numbers computed the same way."""
    rows = []
    for c in candidates:
        rows.append({
            "name": c["player_name"], "team": c["team"],
            "history": bool(c.get("has_history")),
            "injury": bool(c.get("has_injury")),
            "news": bool(c.get("has_news")),
            "predictedXi": bool(c.get("has_predicted_xi")),
            "x": bool(c.get("has_x_signal")),
            "starterVotes": c.get("starter_votes", 0), "benchVotes": c.get("bench_votes", 0),
            "unknownVotes": c.get("unknown_votes", 0),
            "sourceCount": sum([bool(c.get("has_history")), bool(c.get("has_injury")),
                                 bool(c.get("has_news")), bool(c.get("has_x_signal"))]),
            "consensus": c.get("prediction_consensus", "unknown"),
        })
    rows.sort(key=lambda r: r["sourceCount"], reverse=True)
    return rows


def _coverage_tiers(candidates):
    n = len(candidates)
    board_only = sum(1 for c in candidates if c.get("board_only"))
    two_plus = sum(1 for c in candidates if c.get("has_multiple_external_sources"))
    one_source = sum(1 for c in candidates
                      if c.get("has_any_external_enrichment") and not c.get("has_multiple_external_sources"))
    fully_enriched = sum(1 for c in candidates if c.get("has_history") and c.get("has_injury")
                          and c.get("has_news") and c.get("has_x_signal"))
    return {"total": n, "boardOnly": board_only, "oneSource": one_source,
            "twoPlusSource": two_plus, "fullyEnriched": fully_enriched}


def tier_counts(candidates):
    """(live, watch, alert, high, critical) - the SINGLE shared
    definition of these tier counts (2026-09-14 item 5: found live that
    soccer_daily_digest.py's Discord message and this dashboard disagreed
    - the digest reported "7 WATCH" using a cumulative dns_score>=70
    count while this module's own chip used an EXCLUSIVE [70,75) band,
    so a board where every notable candidate actually scored 75+ showed
    "7 WATCH" in one place and "0 Watch" in the other, both internally
    "correct" for their own, different definition of the word). watch/
    alert are mutually exclusive bands ([70,75)/[75,85)) that partition
    with high (85+, cumulative since it's the top tier) - critical is a
    separate, orthogonal classification (official_status, not
    dns_score), so a candidate can be counted in both high and critical.
    soccer_daily_digest.py imports this directly rather than keeping its
    own parallel copy, so the two can no longer drift apart."""
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
    # Hard_out research block (2026-09-16 item 3) - rendered as a native
    # tooltip (title attribute) on the RotoWire cell rather than a new
    # column, since this dashboard has no per-row expand mechanic yet;
    # cheap and always readable, even though a real expand/detail panel
    # would look nicer.
    research_tooltip = None
    rb = c.get("hard_out_research_block")
    if rb:
        try:
            import soccer_alerts
            research_tooltip = soccer_alerts._format_research_block(rb)
        except Exception:
            research_tooltip = None
    return {
        "dns": c["dns_score"], "conf": c["confidence_score"], "urg": c["urgency_score"],
        "pri": c.get("combined_priority"), "name": c["player_name"], "team": c["team"],
        "matchup": c.get("matchup") or c["team"], "league": c.get("league_code") or c.get("league_raw") or "?",
        "kickoff": _game_time_pt(c.get("event_date")), "dabble": dabble, "rotowire": rw,
        "predictedXi": xi, "news": news, "reasons": reasons, "officialStatus": c.get("official_status"),
        "researchTooltip": research_tooltip,
        "isLock": bool(c.get("is_lock")), "lockReason": c.get("lock_reason"),
    }


def _fixture_freshness_inputs(candidates, now):
    """(any_upcoming, fixture_within_24h) - item 3, 2026-09-16: the
    merged Last Refresh status needs to know not just how old this
    render is, but whether that age actually matters right now (nothing
    upcoming = stale is harmless; a fixture inside 24h = stale is
    urgent). Same "skip rows with an unparseable/missing kickoff rather
    than guess" discipline as soccer_scheduler.py."""
    any_upcoming = False
    fixture_within_24h = False
    for c in candidates:
        raw = c.get("event_date")
        if not raw:
            continue
        try:
            kickoff = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if kickoff <= now:
            continue
        any_upcoming = True
        if (kickoff - now).total_seconds() <= 24 * 3600:
            fixture_within_24h = True
            break
    return any_upcoming, fixture_within_24h


def generate(candidates, date_str=None, out_path=os.path.join("docs", "soccer-dns.html")):
    from soccer_dates import soccer_today_str
    date_str = date_str or soccer_today_str()
    now = datetime.now(timezone.utc)
    any_upcoming, fixture_within_24h = _fixture_freshness_inputs(candidates, now)

    # LOCK-aware ordering (2026-09-17, item 1) - a LOCK candidate ranks
    # above every additive-score candidate regardless of combined_
    # priority; see soccer_dns_score.lock_aware_sort_key.
    import soccer_dns_score
    candidates = sorted(candidates, key=soccer_dns_score.lock_aware_sort_key)
    live, watch, alert, high, critical = tier_counts(candidates)
    lock_count = sum(1 for c in candidates if c.get("is_lock"))
    alert_records, alerts_by_player_fixture = _alert_history(date_str)
    removed = _removed_candidates(date_str, alerts_by_player_fixture)
    source_health = _source_health(candidates)
    coverage_matrix = _coverage_matrix(candidates)
    coverage_tiers = _coverage_tiers(candidates)
    digest_record = _load_json(os.path.join(DIGEST_DIR, f"{date_str}.json"))

    try:
        import discord_health
        discord_health_state = discord_health.get_health()
    except Exception:
        discord_health_state = {}

    rows_js = json.dumps([_candidate_row(c) for c in candidates], ensure_ascii=False)
    removed_js = json.dumps(removed, ensure_ascii=False)
    alerts_js = json.dumps(alert_records, ensure_ascii=False)
    health_js = json.dumps(source_health, ensure_ascii=False)
    digest_js = json.dumps(digest_record, ensure_ascii=False)
    matrix_js = json.dumps(coverage_matrix, ensure_ascii=False)
    tiers_js = json.dumps(coverage_tiers, ensure_ascii=False)
    discord_health_js = json.dumps(discord_health_state, ensure_ascii=False)

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
  .status-chip.lock {{ border-color: #f1c40f; }}
  .status-chip.lock .value {{ color: #f1c40f; }}
  .status-chip.watch .value {{ color: #f1c40f; }}
  .status-chip.alert .value {{ color: #e67e22; }}
  .status-chip.high .value {{ color: #e74c3c; }}
  .status-chip.critical .value {{ color: #8e44ad; }}
  .status-chip.ok .value {{ color: #2ecc71; }}
  .status-chip.bad .value {{ color: #e74c3c; }}
  .status-chip.fresh .value {{ color: #2ecc71; }}
  .status-chip.warn .value {{ color: #f1c40f; }}
  .status-chip.stale .value {{ color: #e74c3c; }}

  h2.section-title {{ font-size: 16px; margin: 32px 0 12px; border-bottom: 1px solid #1c2944; padding-bottom: 6px; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #1c2944; white-space: nowrap; }}
  th {{ color: #9fb0cc; font-weight: 700; cursor: pointer; position: sticky; top: 0; background: #0d1626; }}
  td.reasons, td.news {{ white-space: normal; max-width: 320px; }}
  tr:hover td {{ background: #16213a; }}
  .tier-high {{ color: #e74c3c; font-weight: 800; }}
  .tier-alert {{ color: #e67e22; font-weight: 800; }}
  .tier-watch {{ color: #f1c40f; font-weight: 800; }}
  /* LOCK tier (2026-09-17, item 1) - a confirmed-not-starting or
     uncontradicted hard_out candidate, ranked above every additive-
     score candidate regardless of dns_score. */
  tr.lock-row td {{ background: #241a05; }}
  tr.lock-row:hover td {{ background: #2e2208; }}
  .lock-badge {{
    display: inline-block; background: #f1c40f; color: #241a05; font-weight: 900;
    font-size: 10px; letter-spacing: 0.5px; border-radius: 4px; padding: 2px 6px; margin-right: 4px;
  }}

  .health-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }}
  .health-card {{ background: #16213a; border: 1px solid #2a3a5c; border-radius: 8px; padding: 12px 14px; }}
  .health-card .name {{ font-weight: 700; margin-bottom: 4px; }}
  .health-card .state {{ font-size: 12px; }}
  .health-card .state.on {{ color: #2ecc71; }}
  .health-card .state.off {{ color: #e74c3c; }}
  .empty-msg {{ color: #6b7a99; padding: 16px 0; }}
  .table-wrap {{ overflow-x: auto; }}
  .yes {{ color: #2ecc71; font-weight: 800; }}
  .no {{ color: #4a5876; }}
  .tier-summary {{ display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }}
  .tier-summary .status-chip {{ min-width: 140px; }}
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
    <div class="status-chip lock"><div class="value" id="chipLock">{lock_count}</div><div class="label">LOCK</div></div>
    <div class="status-chip watch"><div class="value" id="chipWatch">{watch}</div><div class="label">Watch (70-74)</div></div>
    <div class="status-chip alert"><div class="value" id="chipAlert">{alert}</div><div class="label">Alert (75-84)</div></div>
    <div class="status-chip high"><div class="value" id="chipHigh">{high}</div><div class="label">High (85+)</div></div>
    <div class="status-chip critical"><div class="value" id="chipCritical">{critical}</div><div class="label">Critical</div></div>
    <div class="status-chip"><div class="value" id="chipRemoved">{len(removed)}</div><div class="label">Removed Today</div></div>
    <div class="status-chip {'ok' if source_health['discord']['enabled'] else 'bad'}"><div class="value">{'ON' if source_health['discord']['enabled'] else 'OFF'}</div><div class="label">Discord</div></div>
    <div class="status-chip {'ok' if source_health['x']['enabled'] else 'bad'}"><div class="value">{'ON' if source_health['x']['enabled'] else 'OFF'}</div><div class="label">X Realtime</div></div>
    <div class="status-chip" id="lastRefreshChip"><div class="value" id="lastRefreshValue" style="font-size:13px">{now.strftime('%I:%M %p UTC')}</div><div class="label">Last Refresh</div></div>
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

  <h2 class="section-title">Coverage Matrix</h2>
  <div class="tier-summary">
    <div class="status-chip"><div class="value" id="tierTotal">0</div><div class="label">Total</div></div>
    <div class="status-chip"><div class="value" id="tierBoardOnly">0</div><div class="label">Board-Only</div></div>
    <div class="status-chip"><div class="value" id="tierOneSource">0</div><div class="label">One-Source</div></div>
    <div class="status-chip"><div class="value" id="tierTwoPlus">0</div><div class="label">Two-Plus-Source</div></div>
    <div class="status-chip"><div class="value" id="tierFullyEnriched">0</div><div class="label">Fully Enriched</div></div>
  </div>
  <div class="table-wrap">
  <table id="matrixTable">
    <thead><tr>
      <th>Player</th><th>Team</th><th>History</th><th>Injury (TM)</th><th>News (RotoWire)</th>
      <th>Predicted XI</th><th>X</th><th># Sources</th><th>Start Votes</th><th>Bench Votes</th><th>Unknown Votes</th>
    </tr></thead>
    <tbody id="matrixBody"></tbody>
  </table>
  </div>

  <h2 class="section-title">Source Health</h2>
  <div class="health-grid" id="healthGrid"></div>

  <h2 class="section-title">Discord Health</h2>
  <div class="health-grid" id="discordHealthGrid"></div>

</main>
<script>
const LIVE = {rows_js};
const REMOVED = {removed_js};
const ALERTS = {alerts_js};
const HEALTH = {health_js};
const DIGEST = {digest_js};
const MATRIX = {matrix_js};
const TIERS = {tiers_js};
const DISCORD_HEALTH = {discord_health_js};
const GENERATED_AT = {json.dumps(now.isoformat())};
const ANY_UPCOMING_FIXTURE = {json.dumps(any_upcoming)};
const FIXTURE_WITHIN_24H = {json.dumps(fixture_within_24h)};

// --- Last Refresh: merged three-tier status (2026-09-16 item 3) - same
// approach as report.py's MLB freshness banner, mirrored here after
// finding live that this chip showed clock-time-only with no date (so
// a 24h-old Friday-evening render could look identical to a fresh one)
// and no color/urgency signal at all. Tiers:
//   green - <=45 min old, or nothing upcoming to go stale on
//   red   - >2h old AND a fixture kicks off within 24h (urgent)
//   amber - stale-ish (>45min) with something still upcoming, but not
//           yet the 2h/24h red case
function formatAge(ageMinutes) {{
  const h = Math.floor(ageMinutes / 60);
  const m = Math.round(ageMinutes % 60);
  if (h <= 0) return `${{m}}m ago`;
  return `${{h}}h ${{m}}m ago`;
}}

function updateLastRefresh() {{
  const generated = new Date(GENERATED_AT);
  const now = new Date();
  const ageMinutes = (now - generated) / 60000;
  const ageStr = formatAge(ageMinutes);
  const dateTimeStr = generated.toLocaleString('en-US', {{
    timeZone: 'America/Los_Angeles', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', hour12: true,
  }}) + ' PT';

  let tier;
  if (ageMinutes <= 45 || !ANY_UPCOMING_FIXTURE) {{
    tier = 'fresh';
  }} else if (ageMinutes > 120 && FIXTURE_WITHIN_24H) {{
    tier = 'stale';
  }} else {{
    tier = 'warn';
  }}

  const chip = document.getElementById('lastRefreshChip');
  const value = document.getElementById('lastRefreshValue');
  if (chip) chip.className = 'status-chip ' + tier;
  if (value) value.textContent = `${{dateTimeStr}} (${{ageStr}})`;
}}
updateLastRefresh();
setInterval(updateLastRefresh, 60000);

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
    if (c.isLock) tr.className = 'lock-row';
    const lockBadge = c.isLock ? '<span class="lock-badge" title="LOCK - ranked above every additive-score candidate">LOCK</span> ' : '';
    tr.innerHTML = `
      <td class="${{tierClass(c.dns)}}">${{c.dns}}</td><td>${{c.conf}}</td><td>${{c.urg}}</td><td>${{c.pri}}</td>
      <td>${{lockBadge}}${{c.name}}</td><td>${{c.matchup}}</td><td>${{c.league}}</td><td>${{c.kickoff}}</td>
      <td>${{c.dabble}}</td><td class="rotowire-cell">${{c.rotowire}}</td><td>${{c.predictedXi}}</td>
      <td class="news">${{c.news}}</td><td class="reasons">${{c.reasons}}</td>`;
    if (c.researchTooltip) {{
      // Set as a DOM property (never innerHTML) so external RotoWire/
      // Transfermarkt injury text can't inject markup (2026-09-16 item 3).
      const rwCell = tr.querySelector('.rotowire-cell');
      rwCell.title = c.researchTooltip;
      rwCell.style.textDecoration = 'underline dotted';
      rwCell.style.cursor = 'help';
    }}
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
    if (a.is_lock) tr.className = 'lock-row';
    const lockBadge = a.is_lock ? '<span class="lock-badge" title="LOCK">LOCK</span> ' : '';
    tr.innerHTML = `<td>${{a.alerted_at || ''}}</td><td>${{lockBadge}}${{a.alert_type}}</td><td>${{a.player_name}}</td>
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

// --- Coverage matrix (item 5) ---------------------------------------
document.getElementById('tierTotal').textContent = TIERS.total || 0;
document.getElementById('tierBoardOnly').textContent = TIERS.boardOnly || 0;
document.getElementById('tierOneSource').textContent = TIERS.oneSource || 0;
document.getElementById('tierTwoPlus').textContent = TIERS.twoPlusSource || 0;
document.getElementById('tierFullyEnriched').textContent = TIERS.fullyEnriched || 0;

const matrixBody = document.getElementById('matrixBody');
function cell(v) {{ return v ? '<span class="yes">&#10003;</span>' : '<span class="no">&mdash;</span>'; }}
if (MATRIX.length === 0) {{
  matrixBody.innerHTML = '<tr><td colspan="11" class="empty-msg">No candidates to show coverage for.</td></tr>';
}} else {{
  for (const m of MATRIX) {{
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${{m.name}}</td><td>${{m.team}}</td>
      <td>${{cell(m.history)}}</td><td>${{cell(m.injury)}}</td><td>${{cell(m.news)}}</td>
      <td>${{cell(m.predictedXi)}}</td><td>${{cell(m.x)}}</td>
      <td>${{m.sourceCount}}</td><td>${{m.starterVotes}}</td><td>${{m.benchVotes}}</td><td>${{m.unknownVotes}}</td>`;
    matrixBody.appendChild(tr);
  }}
}}

// --- Discord health (item 7) -----------------------------------------
const discordHealthGrid = document.getElementById('discordHealthGrid');
const dh = DISCORD_HEALTH || {{}};
function dhCard(name, value, cls) {{
  const card = document.createElement('div');
  card.className = 'health-card';
  card.innerHTML = `<div class="name">${{name}}</div><div class="state ${{cls || ''}}">${{value != null ? value : 'never'}}</div>`;
  return card;
}}
discordHealthGrid.appendChild(dhCard('Last Attempt', dh.last_attempt_at ? `${{dh.last_attempt_at}} (${{dh.last_attempt_source || ''}})` : null));
discordHealthGrid.appendChild(dhCard('Last Successful Delivery', dh.last_success_at ? `${{dh.last_success_at}} (${{dh.last_success_source || ''}})` : null, 'on'));
discordHealthGrid.appendChild(dhCard('Last Failure', dh.last_failure_at ? `${{dh.last_failure_at}} (${{dh.last_failure_source || ''}})` : null, dh.last_failure_at ? 'off' : ''));
discordHealthGrid.appendChild(dhCard('Last Error', dh.last_error || null, dh.last_error ? 'off' : ''));
</script>
</body>
</html>
"""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path
