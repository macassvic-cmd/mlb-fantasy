"""
The Play Card (2026-09-16) - one card per slate that ties Soccer DNS and
the football SGP stacks together. Outputs: Discord embeds (webhook),
docs/sgp.html (the SGP tab, mobile-first), data/oddsblaze/playcard/latest.json.

Read-only consumers, no new scraping:
  * DNS plays come straight from data/soccer_dnp_live_snapshots/<date>.json
    (written by the Soccer DNS pipeline - nothing there is modified or
    re-scored here). Kickoff gate: a fixture that has already kicked off is
    never shown. Corroborating sources are listed exactly as present on the
    candidate (official lineup, Transfermarkt, RotoWire, X, predicted XI);
    nothing is inferred.
  * Stacks come from stack_forge results (data/oddsblaze/playcard/stacks/),
    priced live when a key is available, otherwise the last saved prices
    with every price marked STALE.

Freshness (Pacific times on the card): DNS last refresh = the snapshot's
_latest_batch_ts; SGP prices pulled at = the newest sgp_at across results.
Tiers: fresh <= 45 min, warn <= 120 min, stale beyond that or whenever the
prices are replayed/saved rather than live. The Discord card carries a
warning line whenever either side is not fresh.

No secrets in output: every string that could carry one (error text,
URLs) goes through discord_health.redact(); the OddsBlaze key never
enters this module at all (oddsblaze_client keeps it).

CLI (from the repo root):
    python play_card.py --league nfl --price            # price live (needs ODDSBLAZE_API_KEY), build page + card
    python play_card.py --league nfl,ncaaf --from-saved # rebuild from saved prices, marked STALE
    python play_card.py --league nfl --price --post-discord
"""

import argparse
import html
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

try:
    from discord_health import redact
except Exception:  # pragma: no cover
    def redact(text):
        return text

import stack_forge

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):  # pragma: no cover
    pass
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("play_card")

PACIFIC = ZoneInfo("America/Los_Angeles")
SNAPSHOTS_DIR = os.path.join("data", "soccer_dnp_live_snapshots")
CARD_DIR = os.path.join("data", "oddsblaze", "playcard")
CARD_PATH = os.path.join(CARD_DIR, "latest.json")
PAGE_PATH = os.path.join("docs", "sgp.html")
WEBHOOK_ENV = "DISCORD_SGP_WEBHOOK_URL"
FALLBACK_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"

TOP_DNS = 5
TOP_STACKS_PER_GAME = 3
FRESH_MINUTES = 45
WARN_MINUTES = 120
DISCORD_EMBEDS_PER_MSG = 10
DISCORD_DESC_LIMIT = 3800


# ----- helpers -----------------------------------------------------------------------

def _parse(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def pacific(iso, with_date=True):
    dt = _parse(iso)
    if not dt:
        return "?"
    local = dt.astimezone(PACIFIC)
    hour12 = local.hour % 12 or 12
    t = f"{hour12}:{local.minute:02d} {local.strftime('%p')} PT"
    return f"{local.strftime('%a')} {local.month}/{local.day} {t}" if with_date else t


def age_minutes(iso, now=None):
    dt = _parse(iso)
    if not dt:
        return None
    return ((now or datetime.now(timezone.utc)) - dt).total_seconds() / 60


def tier_for(age_min, forced_stale=False):
    if forced_stale or age_min is None:
        return "stale"
    if age_min <= FRESH_MINUTES:
        return "fresh"
    if age_min <= WARN_MINUTES:
        return "warn"
    return "stale"


# ----- DNS plays (read-only) --------------------------------------------------------------

def latest_snapshot_path(snapshots_dir=SNAPSHOTS_DIR):
    if not os.path.isdir(snapshots_dir):
        return None
    files = sorted(f for f in os.listdir(snapshots_dir) if f.endswith(".json"))
    return os.path.join(snapshots_dir, files[-1]) if files else None


def _sources(latest):
    out = []
    if latest.get("official_status") and latest["official_status"] != "not_yet_posted":
        out.append(f"official lineup: {latest['official_status']}")
    tm = latest.get("transfermarkt_injury")
    if tm:
        out.append("Transfermarkt: " + (tm.get("reason") if isinstance(tm, dict) and tm.get("reason") else "listed"))
    rw = latest.get("rotowire_status_normalized") or latest.get("rotowire_page_tag") or latest.get("rotowire_status_raw")
    if rw:
        out.append(f"RotoWire: {rw}")
    if latest.get("rotowire_injury"):
        out.append(f"RotoWire injury: {latest['rotowire_injury']}")
    if latest.get("x_signal"):
        out.append("X signal")
    bench = latest.get("predicted_bench_sources") or []
    if bench:
        out.append("predicted bench: " + ", ".join(str(b) for b in bench[:3]))
    return out


def top_dns_plays(snapshot_path=None, now=None, limit=TOP_DNS):
    """({plays: [...], last_refresh: iso, snapshot: path}) - kickoff-gated,
    sorted by DNS score then confidence. Never scores anything itself."""
    now = now or datetime.now(timezone.utc)
    path = snapshot_path or latest_snapshot_path()
    if not path or not os.path.exists(path):
        return {"plays": [], "last_refresh": None, "snapshot": None}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    plays = []
    for key, entry in data.items():
        if not key.startswith("pid:") or not isinstance(entry, dict):
            continue
        latest = entry.get("latest") or {}
        hist = entry.get("score_history") or []
        last = hist[-1] if hist else {}
        kickoff = _parse(latest.get("event_date"))
        if not kickoff or kickoff <= now:
            continue  # kickoff gate
        dns = last.get("dns_score", latest.get("dns_score"))
        if dns is None:
            continue
        plays.append({
            "player": latest.get("player_name") or entry.get("name"),
            "team": latest.get("team") or entry.get("team"),
            "matchup": latest.get("matchup"),
            "league": latest.get("league_code") or latest.get("league_raw"),
            "kickoff": latest.get("event_date"),
            "kickoff_pt": pacific(latest.get("event_date")),
            "dns": dns,
            "confidence": last.get("confidence_score", latest.get("confidence_score")),
            "sources": _sources(latest),
            "rotowire_url": latest.get("rotowire_player_page_url"),
            "scored_at": last.get("ts"),
        })
    plays.sort(key=lambda p: (-(p["dns"] or 0), -(p["confidence"] or 0), p["kickoff"] or ""))
    return {"plays": plays[:limit], "last_refresh": data.get("_latest_batch_ts"), "snapshot": path}


# ----- card assembly ------------------------------------------------------------------------

def _game_card(r, now, top_n=TOP_STACKS_PER_GAME, forced_stale=False):
    stacks = []
    for s in r.get("stacks", [])[:top_n]:
        if not s.get("best_book"):
            continue
        replayed = any(b.get("replayed_from") for b in s["books"])
        stacks.append({
            "name": s["name"],
            "legs": [{"player": l["player"], "team": l["team"], "slot": l["slot"], "market": stack_forge.MARKET_SHORT.get(l["market"], l["market"]), "side": l["side"],
                      "position_flag": l.get("position_flag"),
                      "lines": {stack_forge.BOOK_ABBR.get(b["book"], b["book"]): (b["legs"][i]["line"] if b["legs"][i] else None) for b in s["books"]}}
                     for i, l in enumerate(s["legs"])],
            "best_book": s["best_book"], "best_book_abbr": stack_forge.BOOK_ABBR.get(s["best_book"], s["best_book"]),
            "best_american": s["best_american"], "best_decimal": s["best_decimal"], "best_link": redact(s.get("best_link")),
            "runner_up_book": s.get("runner_up_book"), "runner_up_abbr": stack_forge.BOOK_ABBR.get(s.get("runner_up_book"), s.get("runner_up_book")),
            "runner_up_decimal": s.get("runner_up_decimal"), "spread_pct": s.get("spread_pct"), "books_priced": s.get("books_priced"),
            "books": [{"book": b["book"], "abbr": stack_forge.BOOK_ABBR.get(b["book"], b["book"]), "priced": b.get("priced"), "american": b.get("american"), "decimal": b.get("decimal"),
                       "link": redact(b.get("link")), "missing": b.get("missing") or [], "error": redact(b.get("error")) if b.get("error") else None, "notes": b.get("notes") or []} for b in s["books"]],
            "stale": forced_stale or replayed,
        })
    sgp_times = [b["sgp_at"] for s in r.get("stacks", []) for b in s["books"] if b.get("sgp_at")]
    return {
        "league": r["league"], "event": r["event"], "away": r["away"], "home": r["home"], "kickoff": r["kickoff"], "kickoff_pt": pacific(r["kickoff"]),
        "stacks": stacks, "notes": [redact(n) for n in r.get("notes", [])], "budget_trimmed": bool(r.get("budget_trimmed")),
        "prices_pulled_at": max(sgp_times) if sgp_times else None,
        "best_overall": dict(r["best_overall"], link=redact(r["best_overall"].get("link"))) if r.get("best_overall") else None,
        "roster": {t: {slot: ({"name": p["name"], "line": p.get("dk_line"), "flag": p.get("position_flag")} if p else None) for slot, p in slots.items()} for t, slots in (r.get("roster") or {}).items()},
    }


def build_card(leagues=("nfl", "ncaaf"), results=None, dns=None, now=None, forced_stale=False, budget=None, source="live"):
    now = now or datetime.now(timezone.utc)
    results = results if results is not None else [r for lg in leagues for r in stack_forge.load_results(league=lg, max_age_days=2)]
    games = []
    for r in results:
        ko = _parse(r.get("kickoff"))
        if ko and ko <= now:
            continue  # kickoff gate
        games.append(_game_card(r, now, forced_stale=forced_stale))
    games.sort(key=lambda g: (g["kickoff"] or "", g["away"]))
    dns = dns if dns is not None else top_dns_plays(now=now)
    sgp_pulled = max((g["prices_pulled_at"] for g in games if g["prices_pulled_at"]), default=None)
    dns_age = age_minutes(dns.get("last_refresh"), now)
    sgp_age = age_minutes(sgp_pulled, now)
    dns_tier = tier_for(dns_age)
    sgp_tier = tier_for(sgp_age, forced_stale=forced_stale or any(s["stale"] for g in games for s in g["stacks"]))
    warnings = []
    if dns_tier != "fresh":
        warnings.append(f"DNS data is {dns_tier.upper()} (last refresh {pacific(dns.get('last_refresh'))})")
    if sgp_tier != "fresh":
        warnings.append(f"SGP prices are {sgp_tier.upper()}" + (f" (pulled {pacific(sgp_pulled)})" if sgp_pulled else " (no live prices)") + (" - shown from last saved prices" if forced_stale else ""))
    if any(g["budget_trimmed"] for g in games):
        warnings.append("budget guard trimmed some variants - see game notes")
    top_stacks = sorted([dict(s, game=f'{g["away"]}@{g["home"]}', kickoff_pt=g["kickoff_pt"]) for g in games for s in g["stacks"]], key=lambda s: -(s["best_decimal"] or 0))[:TOP_STACKS_PER_GAME]
    return {
        "generated_at": now.isoformat(), "generated_at_pt": pacific(now.isoformat()), "source": source,
        "dns": {"plays": dns.get("plays", []), "last_refresh": dns.get("last_refresh"), "last_refresh_pt": pacific(dns.get("last_refresh")), "age_minutes": dns_age, "tier": dns_tier},
        "sgp": {"pulled_at": sgp_pulled, "pulled_at_pt": pacific(sgp_pulled) if sgp_pulled else "never", "age_minutes": sgp_age, "tier": sgp_tier, "leagues": list(leagues)},
        "games": games, "top_stacks": top_stacks, "warnings": warnings, "budget": budget,
        "overall_tier": "stale" if "stale" in (dns_tier, sgp_tier) else ("warn" if "warn" in (dns_tier, sgp_tier) else "fresh"),
    }


def save_card(card, path=CARD_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(card, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    return path


# ----- Discord ---------------------------------------------------------------------------------

def _fmt_price(american, decimal):
    return f"{american:+d} ({decimal:.2f})" if american is not None and decimal else "n/a"


def discord_embeds(card):
    tier_color = {"fresh": 0x2ECC71, "warn": 0xF1C40F, "stale": 0xE74C3C}[card["overall_tier"]]
    head = f"Generated {card['generated_at_pt']} · DNS refresh {card['dns']['last_refresh_pt']} · SGP prices {card['sgp']['pulled_at_pt']}"
    if card["warnings"]:
        head += "\n⚠ " + "\n⚠ ".join(card["warnings"])
    embeds = [{"title": "The Play Card", "description": head[:DISCORD_DESC_LIMIT], "color": tier_color}]
    dns_lines = []
    for p in card["dns"]["plays"]:
        src = "; ".join(p["sources"]) if p["sources"] else "no corroborating source yet"
        link = f" [RotoWire]({p['rotowire_url']})" if p.get("rotowire_url") else ""
        dns_lines.append(f"**{p['player']}** ({p['team']}, {p.get('matchup') or '?'}) — kickoff {p['kickoff_pt']} — DNS **{p['dns']}** / conf {p['confidence']} — {src}{link}")
    embeds.append({"title": f"Top DNS plays ({card['dns']['tier'].upper()})", "description": ("\n".join(dns_lines) or "No upcoming DNS candidates.")[:DISCORD_DESC_LIMIT], "color": tier_color})
    for g in card["games"]:
        lines = []
        for s in g["stacks"]:
            legs = " + ".join(f"{l['player']} {l['market']} O" for l in s["legs"])
            best = f"**{s['best_book_abbr']} {_fmt_price(s['best_american'], s['best_decimal'])}**"
            ru = f", runner-up {s['runner_up_abbr']} {s['runner_up_decimal']:.2f} (spread {s['spread_pct']:+.1f}%)" if s.get("runner_up_book") else ""
            stale = " ⚠ STALE" if s["stale"] else ""
            link = f" [bet]({s['best_link']})" if s.get("best_link") else ""
            lines.append(f"**{s['name']}** — {legs}\n{best}{ru}{stale}{link}")
        if g["notes"]:
            lines.append("_" + "; ".join(g["notes"]) + "_")
        embeds.append({"title": f"{g['away']} @ {g['home']} · {g['league'].upper()} · {g['kickoff_pt']}", "description": ("\n".join(lines) or "No priced stacks.")[:DISCORD_DESC_LIMIT], "color": tier_color})
    return [{"title": redact(e["title"]), "description": redact(e["description"]), "color": e["color"]} for e in embeds]


DISCORD_MSG_CHAR_BUDGET = 5500  # Discord caps one message at 6000 chars across all embeds (HTTP 400 otherwise)


def chunk_embeds(embeds):
    """Split embeds into messages of <= DISCORD_EMBEDS_PER_MSG embeds and
    <= DISCORD_MSG_CHAR_BUDGET total characters (title + description)."""
    batches, cur, size = [], [], 0
    for e in embeds:
        n = len(e.get("title") or "") + len(e.get("description") or "")
        if cur and (len(cur) >= DISCORD_EMBEDS_PER_MSG or size + n > DISCORD_MSG_CHAR_BUDGET):
            batches.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += n
    if cur:
        batches.append(cur)
    return batches


def post_latest(card_path=CARD_PATH):
    """Re-post the last built card without re-pricing anything."""
    with open(card_path, encoding="utf-8") as f:
        return post_discord(json.load(f))


def post_discord(card, webhook_url=None):
    import discord_health
    url = webhook_url or os.environ.get(WEBHOOK_ENV) or os.environ.get(FALLBACK_WEBHOOK_ENV)
    if not url:
        logger.warning("no Discord webhook configured (%s / %s) - not posting", WEBHOOK_ENV, FALLBACK_WEBHOOK_ENV)
        return False
    embeds = discord_embeds(card)
    ok = True
    for batch in chunk_embeds(embeds):
        try:
            resp = requests.post(url, json={"embeds": batch}, timeout=15)
            resp.raise_for_status()
            discord_health.record_attempt("play_card", True, http_status=resp.status_code)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            logger.error("play_card: Discord webhook POST failed: %s (HTTP %s)", type(e).__name__, status)
            discord_health.record_attempt("play_card", False, http_status=status, error=f"{type(e).__name__} (HTTP {status})")
            ok = False
    return ok


# ----- HTML (the SGP tab) --------------------------------------------------------------------------

CSS = """
  * { box-sizing: border-box; }
  body { margin: 0; background: #0d1626; color: #e6edf7; font-family: -apple-system, system-ui, Segoe UI, Roboto, sans-serif; font-size: 15px; }
  header { padding: 14px 16px 6px; }
  header h1 { margin: 0; font-size: 20px; }
  .meta { color: #9fb0cc; font-size: 13px; margin-top: 4px; }
  .subnav { padding: 8px 16px; background: #0a1120; border-bottom: 1px solid #1c2944; display: flex; gap: 16px; overflow-x: auto; white-space: nowrap; }
  .subnav a { color: #9fb0cc; text-decoration: none; font-size: 14px; }
  .subnav a.active { color: #fff; font-weight: 700; }
  .freshness-banner { padding: 10px 16px; font-size: 14px; font-weight: 700; text-align: center; }
  .freshness-banner.fresh { background: #15351f; color: #4ade80; }
  .freshness-banner.warn { background: #3a2e10; color: #fbbf24; }
  .freshness-banner.stale { background: #3a1818; color: #f87171; }
  main { padding: 12px 12px 40px; max-width: 1100px; margin: 0 auto; }
  h2 { font-size: 16px; margin: 22px 0 10px; border-bottom: 1px solid #1c2944; padding-bottom: 6px; }
  .card { background: #16213a; border: 1px solid #2a3a5c; border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; }
  .card .title { font-weight: 800; font-size: 15px; }
  .card .sub { color: #9fb0cc; font-size: 13px; margin-top: 2px; }
  .kv { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 6px; font-size: 13px; }
  .kv b { color: #fff; }
  .legs { margin: 8px 0 0; padding: 0; list-style: none; }
  .legs li { padding: 6px 0; border-top: 1px solid #1c2944; font-size: 13.5px; }
  .legs li .who { font-weight: 700; }
  .lines { color: #9fb0cc; font-size: 12px; margin-top: 2px; }
  .lines span { display: inline-block; margin-right: 8px; }
  .books { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
  .book { background: #0d1626; border: 1px solid #2a3a5c; border-radius: 8px; padding: 6px 8px; font-size: 12.5px; min-width: 92px; }
  .book.best { border-color: #2ecc71; background: #15351f; }
  .book.runner { border-color: #f1c40f; }
  .book.stale { opacity: 0.75; }
  .book .abbr { color: #9fb0cc; font-size: 11px; display: block; }
  .book .price { font-weight: 800; }
  .book .miss { color: #f87171; font-size: 11px; white-space: normal; }
  .badge { display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 11px; font-weight: 700; margin-left: 6px; }
  .badge.stale { background: #3a1818; color: #f87171; }
  .badge.best { background: #15351f; color: #4ade80; }
  .flag { color: #fbbf24; font-size: 12px; }
  .note { color: #fbbf24; font-size: 12.5px; margin-top: 6px; }
  .warn-list { color: #fbbf24; font-size: 13px; margin: 8px 0 0; padding-left: 18px; }
  a { color: #7fb3ff; }
  .btn { display: inline-block; margin-top: 6px; padding: 6px 10px; border-radius: 8px; background: #1f6feb; color: #fff; text-decoration: none; font-size: 13px; font-weight: 700; }
  .dns-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; align-items: start; }
  .score { font-size: 22px; font-weight: 900; text-align: right; }
  .score.high { color: #e74c3c; } .score.alert { color: #e67e22; } .score.watch { color: #f1c40f; }
  .empty { color: #6b7a99; padding: 10px 0; }
  @media (min-width: 720px) { body { font-size: 14px; } .games { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; } .games .card { margin-bottom: 0; } }
"""


def _score_class(dns):
    try:
        d = float(dns)
    except (TypeError, ValueError):
        return ""
    return "high" if d >= 85 else "alert" if d >= 75 else "watch" if d >= 70 else ""


def render_html(card, out_path=PAGE_PATH):
    e = html.escape
    parts = []
    parts.append(f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>SGP Play Card</title><style>{CSS}</style></head><body>
<header><h1>The Play Card</h1><div class="meta">Generated {e(card['generated_at_pt'])} &middot; DNS refresh {e(card['dns']['last_refresh_pt'])} &middot; SGP prices pulled {e(card['sgp']['pulled_at_pt'])} &middot; source: {e(card['source'])}</div></header>
<div class="subnav"><a href="index.html">MLB Dashboard</a><a href="soccer-dns.html">Soccer DNS</a><a href="sgp.html" class="active">SGP</a></div>
<div id="freshnessBanner" class="freshness-banner {card['overall_tier']}"></div>
<main>""")
    if card["warnings"]:
        parts.append("<ul class='warn-list'>" + "".join(f"<li>{e(w)}</li>" for w in card["warnings"]) + "</ul>")
    parts.append(f"<h2>Top DNS plays <span class='badge {card['dns']['tier']}'>{card['dns']['tier'].upper()}</span></h2>")
    if not card["dns"]["plays"]:
        parts.append("<div class='empty'>No upcoming DNS candidates in the latest snapshot.</div>")
    for p in card["dns"]["plays"]:
        src = "; ".join(p["sources"]) if p["sources"] else "no corroborating source yet"
        link = f"<a class='btn' href='{e(p['rotowire_url'])}' target='_blank' rel='noopener'>RotoWire</a>" if p.get("rotowire_url") else ""
        parts.append(f"""<div class="card"><div class="dns-row"><div><div class="title">{e(str(p['player']))} <span class="sub">{e(str(p['team']))} &middot; {e(str(p.get('matchup') or ''))} &middot; {e(str(p.get('league') or ''))}</span></div>
<div class="sub">kickoff {e(p['kickoff_pt'])} &middot; confidence {e(str(p['confidence']))}</div><div class="sub">{e(src)}</div>{link}</div>
<div class="score {_score_class(p['dns'])}">{e(str(p['dns']))}<div class="sub" style="font-size:11px;text-align:right">DNS</div></div></div></div>""")
    parts.append(f"<h2>Top 3 SGP stacks overall <span class='badge {card['sgp']['tier']}'>{card['sgp']['tier'].upper()}</span></h2>")
    if not card["top_stacks"]:
        parts.append("<div class='empty'>No priced stacks yet.</div>")
    for s in card["top_stacks"]:
        parts.append(_stack_card_html(s, heading=f"{s['game']} &middot; {e(s['kickoff_pt'])}"))
    parts.append("<h2>Stacks by game</h2><div class='games'>")
    if not card["games"]:
        parts.append("<div class='empty'>No upcoming games priced.</div>")
    for g in card["games"]:
        bo = g.get("best_overall")
        best = f"Best: <b>{e(bo['stack'])}</b> at <b>{e(stack_forge.BOOK_ABBR.get(bo['book'], bo['book']))}</b> {bo['american']:+d} ({bo['decimal']:.2f})" if bo else "No priced stacks"
        notes = "".join(f"<div class='note'>{e(n)}</div>" for n in g["notes"])
        inner = "".join(_stack_card_html(s, heading=None) for s in g["stacks"])
        parts.append(f"<div class='card'><div class='title'>{e(g['away'])} @ {e(g['home'])} <span class='sub'>{e(g['league'].upper())} &middot; kickoff {e(g['kickoff_pt'])}</span></div><div class='sub'>{best}</div>{notes}{inner}</div>")
    parts.append("</div></main>")
    parts.append(f"""<script>
const DNS_AT = {json.dumps(card['dns']['last_refresh'])}; const SGP_AT = {json.dumps(card['sgp']['pulled_at'])}; const FORCED_STALE = {json.dumps(card['sgp']['tier'] == 'stale' and card['source'] != 'live')};
function fmt(iso) {{ if (!iso) return 'never'; return new Date(iso).toLocaleString('en-US', {{timeZone: 'America/Los_Angeles', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true}}) + ' PT'; }}
function tier(iso, forced) {{ if (forced || !iso) return 'stale'; const m = (Date.now() - new Date(iso)) / 60000; return m <= {FRESH_MINUTES} ? 'fresh' : m <= {WARN_MINUTES} ? 'warn' : 'stale'; }}
function refresh() {{ const d = tier(DNS_AT, false), s = tier(SGP_AT, FORCED_STALE); const overall = (d === 'stale' || s === 'stale') ? 'stale' : (d === 'warn' || s === 'warn') ? 'warn' : 'fresh';
  const b = document.getElementById('freshnessBanner'); b.className = 'freshness-banner ' + overall;
  b.textContent = (overall === 'fresh' ? 'FRESH' : overall === 'warn' ? 'AGING' : 'STALE') + ' — DNS ' + d.toUpperCase() + ' (' + fmt(DNS_AT) + ') · SGP prices ' + s.toUpperCase() + ' (' + fmt(SGP_AT) + ')'; }}
refresh(); setInterval(refresh, 60000);
</script></body></html>""")
    out = "".join(parts)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    return out_path


def _stack_card_html(s, heading=None):
    e = html.escape
    legs = []
    for l in s["legs"]:
        lines = " ".join(f"<span>{e(ab)} {ln:g}</span>" for ab, ln in l["lines"].items() if ln is not None)
        flag = f" <span class='flag'>[{e(l['position_flag'])}]</span>" if l.get("position_flag") else ""
        legs.append(f"<li><span class='who'>{e(l['player'])}</span> <span class='sub'>{e(l['team'])} {e(l['slot'])} &middot; {e(l['market'])} {e(l['side'])}</span>{flag}<div class='lines'>{lines}</div></li>")
    books = []
    for b in s["books"]:
        cls = "book" + (" best" if b["book"] == s["best_book"] else " runner" if b["book"] == s.get("runner_up_book") else "") + (" stale" if s["stale"] else "")
        if b["priced"]:
            link = f"<br><a href='{e(b['link'])}' target='_blank' rel='noopener'>bet</a>" if b.get("link") else ""
            books.append(f"<div class='{cls}'><span class='abbr'>{e(b['abbr'])}</span><span class='price'>{b['american']:+d}</span> <span class='sub'>{b['decimal']:.2f}</span>{link}</div>")
        else:
            why = "; ".join(m.split(" (")[0] for m in b["missing"]) if b["missing"] else (b.get("error") or "not priced")
            books.append(f"<div class='{cls}'><span class='abbr'>{e(b['abbr'])}</span><span class='miss'>{e(why)}</span></div>")
    ru = f" &middot; runner-up {e(s['runner_up_abbr'])} {s['runner_up_decimal']:.2f} &middot; spread <b>{s['spread_pct']:+.1f}%</b>" if s.get("runner_up_book") else ""
    stale = "<span class='badge stale'>STALE</span>" if s["stale"] else ""
    link = f"<a class='btn' href='{e(s['best_link'])}' target='_blank' rel='noopener'>Open at {e(s['best_book_abbr'])}</a>" if s.get("best_link") else "<div class='sub'>no deep link returned</div>"
    head = f"<div class='sub'>{heading}</div>" if heading else ""
    return f"""<div class="card"><div class="title">{e(s['name'])} <span class='badge best'>{e(s['best_book_abbr'])} {s['best_american']:+d} ({s['best_decimal']:.2f})</span>{stale}</div>{head}
<div class="kv"><span>implied <b>{100 / s['best_decimal']:.1f}%</b></span><span>books priced <b>{s['books_priced']}</b></span>{ru}</div>
<ul class="legs">{''.join(legs)}</ul><div class="books">{''.join(books)}</div>{link}</div>"""


# ----- driver ---------------------------------------------------------------------------------------

def run(leagues, price=False, from_saved=False, post=False, limit=None, page_path=PAGE_PATH, card_path=CARD_PATH, event_ids=None):
    """Returns (card, page_path). Live pricing degrades to saved prices
    (marked STALE) when the key is missing or rejected."""
    source, forced_stale, budget = "saved", True, None
    if price and not from_saved:
        try:
            client = OddsBlazeClient = __import__("oddsblaze_client").OddsBlazeClient
            c = client()
            for lg in leagues:
                stack_forge.price_slate(c, lg, limit=limit, event_ids=event_ids)
            source, forced_stale, budget = "live", False, c.guard.summary()
        except Exception as e:
            name = type(e).__name__
            logger.error("live pricing unavailable (%s: %s) - building from last saved prices, marked STALE", name, redact(str(e))[:200])
            source = f"saved ({name})"
    card = build_card(leagues, forced_stale=forced_stale, budget=budget, source=source)
    save_card(card, card_path)
    render_html(card, page_path)
    if post:
        post_discord(card)
    return card, page_path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", default="nfl,ncaaf")
    ap.add_argument("--price", action="store_true", help="price stacks live via OddsBlaze (needs ODDSBLAZE_API_KEY)")
    ap.add_argument("--from-saved", action="store_true", help="never call OddsBlaze; use saved prices, marked STALE")
    ap.add_argument("--post-discord", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--events", help="comma-separated event ids")
    ap.add_argument("--out", default=PAGE_PATH)
    ap.add_argument("--post-latest", action="store_true", help="only re-post data/oddsblaze/playcard/latest.json to Discord")
    args = ap.parse_args()
    if args.post_latest:
        print("posted" if post_latest() else "post failed")
        return
    leagues = [l for l in args.league.split(",") if l]
    card, page = run(leagues, price=args.price, from_saved=args.from_saved, post=args.post_discord, limit=args.limit, page_path=args.out, event_ids=args.events.split(",") if args.events else None)
    print(f"card: {CARD_PATH}\npage: {page}\ntier: {card['overall_tier']} | games: {len(card['games'])} | dns plays: {len(card['dns']['plays'])} | warnings: {card['warnings']}")


if __name__ == "__main__":
    main()
