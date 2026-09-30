"""
The Play Card (2026-09-16, rev 3: DraftKings primary) - one card per
football slate. Per game: the LOWEST-ODDS (most correlated) 6-man SGP
stack at DraftKings on DraftKings' main lines, then the other variants in
ranked order. Outputs: Discord embeds (webhook), docs/sgp.html (the SGP
tab, mobile-first), data/oddsblaze/playcard/latest.json.

No DNS on this card or tab - the Soccer DNS tab covers that.
top_dns_plays() stays here only because sgp_bot's /dns uses it.

Stacks come from stack_forge results (data/oddsblaze/playcard/stacks/):
  * every variant is priced at DraftKings only and ranked by lowest SGP
    price = highest implied probability, tie-broken by higher correlation
    (naive product of the leg prices / SGP price; DraftKings' own
    correlation figure shown too).
  * a variant DraftKings could not price ("Price not found", missing leg)
    is shown on the game with the reason - never silently re-priced at
    another book.
  * optional compare (page toggle, OFF by default): other books' prices
    for the lowest-odds stack at the SAME lines only, with the gap vs
    DraftKings.

Freshness (Pacific times): SGP prices pulled at = newest sgp_at across
results. fresh <= 45 min, warn <= 120 min, else stale; replayed/saved-
without-key prices are always stale. Kickoff gate: games that have kicked
off are dropped.

No secrets in output: error text and URLs go through discord_health.redact();
the OddsBlaze key never enters this module (oddsblaze_client keeps it).

CLI (from the repo root):
    python play_card.py --league nfl --price            # price live (needs ODDSBLAZE_API_KEY)
    python play_card.py --league nfl,ncaaf --from-saved # rebuild page + card from saved prices
    python play_card.py --post-latest                   # re-post the saved card to Discord
"""

import argparse
import html
import json
import logging
import os
import sys
from datetime import datetime, timezone
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
FRESH_MINUTES = 45
WARN_MINUTES = 120
DISCORD_EMBEDS_PER_MSG = 10
DISCORD_DESC_LIMIT = 3800
DISCORD_MSG_CHAR_BUDGET = 5500  # Discord caps one message at 6000 chars across all embeds (HTTP 400 otherwise)
ABBR = stack_forge.BOOK_ABBR


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


def _abbr(book):
    return ABBR.get(book, book) if book else None


def _fmt_price(american, decimal):
    return f"{american:+d} ({decimal:.2f})" if american is not None and decimal else "n/a"


# ----- DNS plays (read-only; used by sgp_bot's /dns only, NOT by the card) ---------------

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
    """Kickoff-gated top DNS candidates straight from the live snapshot.
    Never scores or writes anything."""
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
            continue
        dns = last.get("dns_score", latest.get("dns_score"))
        if dns is None:
            continue
        plays.append({"player": latest.get("player_name") or entry.get("name"), "team": latest.get("team") or entry.get("team"), "matchup": latest.get("matchup"),
                      "league": latest.get("league_code") or latest.get("league_raw"), "kickoff": latest.get("event_date"), "kickoff_pt": pacific(latest.get("event_date")),
                      "dns": dns, "confidence": last.get("confidence_score", latest.get("confidence_score")), "sources": _sources(latest),
                      "rotowire_url": latest.get("rotowire_player_page_url"), "scored_at": last.get("ts")})
    plays.sort(key=lambda p: (-(p["dns"] or 0), -(p["confidence"] or 0), p["kickoff"] or ""))
    return {"plays": plays[:limit], "last_refresh": data.get("_latest_batch_ts"), "snapshot": path}


# ----- card assembly ------------------------------------------------------------------------

def _stack_view(s, forced_stale):
    replayed = any(b.get("replayed_from") for b in s["books"])
    dk = next((b for b in s["books"] if b["book"] == "draftkings"), None)
    legs = []
    for i, l in enumerate(s["legs"]):
        dk_leg = dk["legs"][i] if dk and dk.get("legs") and dk["legs"][i] else None
        legs.append({"player": l["player"], "team": l["team"], "slot": l["slot"], "market": stack_forge.MARKET_SHORT.get(l["market"], l["market"]), "side": l["side"],
                     "position_flag": l.get("position_flag"), "line": dk_leg["line"] if dk_leg else l.get("dk_line"), "price": dk_leg["price"] if dk_leg else None})
    compare = [{"book": c["book"], "abbr": _abbr(c["book"]), "american": c["american"], "decimal": c["decimal"], "gap_pct": c["gap_pct"], "link": redact(c.get("link"))} for c in s.get("compare_books") or []]
    skipped = [{"book": b["book"], "abbr": _abbr(b["book"]), "why": b.get("compare_skipped") or ("missing legs" if b.get("missing") else redact(b.get("error")) or "not priced"),
                "lines": [(l or {}).get("line") for l in b.get("legs") or []]} for b in s["books"] if b["book"] != "draftkings" and not b.get("priced")]
    skipped += [{"book": d["book"], "abbr": _abbr(d["book"]), "why": "different lines - not compared", "lines": d["lines"]} for d in s.get("different_line_books") or []]
    return {
        "name": s["name"], "legs": legs, "priced": bool(s.get("reference_book")), "dk_problem": redact(s.get("dk_problem")) if s.get("dk_problem") else None,
        "american": s.get("ref_american"), "decimal": s.get("ref_decimal"), "implied": s.get("implied"),
        "naive_decimal": s.get("naive_decimal"), "correlation_ratio": s.get("correlation_ratio"), "dk_correlation": s.get("dk_correlation"), "link": redact(s.get("ref_link")),
        "compare": compare, "compare_skipped": skipped, "stale": forced_stale or replayed,
    }


def _game_card(r, forced_stale=False):
    views = [_stack_view(s, forced_stale) for s in r.get("stacks", [])]
    priced = [v for v in views if v["priced"]]
    problems = [v for v in views if not v["priced"]]
    sgp_times = [b["sgp_at"] for s in r.get("stacks", []) for b in s["books"] if b.get("sgp_at")]
    return {
        "league": r["league"], "event": r["event"], "away": r["away"], "home": r["home"], "kickoff": r["kickoff"], "kickoff_pt": pacific(r["kickoff"]),
        "lowest": priced[0] if priced else None, "ranked": priced, "dk_problems": [{"name": v["name"], "why": v["dk_problem"]} for v in problems],
        "variants_priced": len(priced), "variants_total": len(views),
        "notes": [redact(n) for n in r.get("notes", [])], "budget_trimmed": bool(r.get("budget_trimmed")),
        "prices_pulled_at": max(sgp_times) if sgp_times else None,
    }


def build_card(leagues=("nfl", "ncaaf"), results=None, now=None, forced_stale=False, budget=None, source="live"):
    now = now or datetime.now(timezone.utc)
    results = results if results is not None else [r for lg in leagues for r in stack_forge.load_results(league=lg, max_age_days=2)]
    games = []
    for r in results:
        ko = _parse(r.get("kickoff"))
        if ko and ko <= now:
            continue  # kickoff gate
        games.append(_game_card(r, forced_stale=forced_stale))
    games.sort(key=lambda g: (g["kickoff"] or "", g["away"]))
    sgp_pulled = max((g["prices_pulled_at"] for g in games if g["prices_pulled_at"]), default=None)
    sgp_age = age_minutes(sgp_pulled, now)
    any_stale_rows = any(v["stale"] for g in games for v in g["ranked"])
    tier = tier_for(sgp_age, forced_stale=forced_stale or any_stale_rows)
    warnings = []
    if tier != "fresh":
        warnings.append(f"SGP prices are {tier.upper()}" + (f" (pulled {pacific(sgp_pulled)})" if sgp_pulled else " (no live prices)") + (" - shown from last saved prices" if forced_stale else ""))
    if any(g["budget_trimmed"] for g in games):
        warnings.append("budget guard trimmed some variants - see game notes")
    if any(g["dk_problems"] for g in games):
        warnings.append("DraftKings could not price some variants - listed per game, not re-priced elsewhere")
    return {
        "generated_at": now.isoformat(), "generated_at_pt": pacific(now.isoformat()), "source": source, "primary": "draftkings",
        "sgp": {"pulled_at": sgp_pulled, "pulled_at_pt": pacific(sgp_pulled) if sgp_pulled else "never", "age_minutes": sgp_age, "tier": tier, "leagues": list(leagues)},
        "games": games, "warnings": warnings, "budget": budget, "overall_tier": tier,
    }


def save_card(card, path=CARD_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(card, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    return path


# ----- Discord ---------------------------------------------------------------------------------

def _legs_line(v):
    return " + ".join(f"{l['player']} {l['market']} O{l['line']:g}" if l.get("line") is not None else f"{l['player']} {l['market']} O" for l in v["legs"])


def _corr_text(v):
    if not v.get("correlation_ratio"):
        return "correlation n/a"
    t = f"correlation {v['correlation_ratio']:.2f}x (naive {v['naive_decimal']:.1f} / SGP {v['decimal']:.2f})"
    if v.get("dk_correlation") is not None:
        t += f", DK corr {v['dk_correlation']:.1f}"
    return t


def discord_embeds(card):
    color = {"fresh": 0x2ECC71, "warn": 0xF1C40F, "stale": 0xE74C3C}[card["overall_tier"]]
    head = f"Generated {card['generated_at_pt']} · SGP prices pulled {card['sgp']['pulled_at_pt']} · DraftKings main lines, ranked by lowest odds"
    if card["warnings"]:
        head += "\n⚠ " + "\n⚠ ".join(card["warnings"])
    embeds = [{"title": "The Play Card — SGP stacks (DraftKings)", "description": head[:DISCORD_DESC_LIMIT], "color": color}]
    for g in card["games"]:
        title = f"{g['away']} @ {g['home']} · {g['league'].upper()} · {g['kickoff_pt']}"
        lines = []
        v = g["lowest"]
        if v:
            lines.append(f"**Lowest-odds stack: {v['name']}** — DK **{_fmt_price(v['american'], v['decimal'])}**, implied **{v['implied'] * 100:.1f}%**, {_corr_text(v)}" + (" ⚠ STALE" if v["stale"] else "") + (f" [bet at DK]({v['link']})" if v.get("link") else ""))
            lines.append(_legs_line(v))
            if len(g["ranked"]) > 1:
                lines.append("Next by odds: " + "; ".join(f"{o['name']} {_fmt_price(o['american'], o['decimal'])} ({o['implied'] * 100:.1f}%, corr {o['correlation_ratio']:.2f}x)" if o.get("correlation_ratio") else f"{o['name']} {_fmt_price(o['american'], o['decimal'])}" for o in g["ranked"][1:]))
        else:
            lines.append("No variant priced at DraftKings.")
        if g["dk_problems"]:
            lines.append("⚠ DraftKings could not price: " + "; ".join(f"{p['name']} ({p['why']})" for p in g["dk_problems"]))
        if g["notes"]:
            lines.append("_" + "; ".join(g["notes"]) + "_")
        embeds.append({"title": title, "description": "\n".join(lines)[:DISCORD_DESC_LIMIT], "color": color})
    return [{"title": redact(e["title"]), "description": redact(e["description"]), "color": e["color"]} for e in embeds]


def chunk_embeds(embeds):
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
    with open(card_path, encoding="utf-8") as f:
        return post_discord(json.load(f))


def post_discord(card, webhook_url=None):
    import discord_health
    url = webhook_url or os.environ.get(WEBHOOK_ENV) or os.environ.get(FALLBACK_WEBHOOK_ENV)
    if not url:
        logger.warning("no Discord webhook configured (%s / %s) - not posting", WEBHOOK_ENV, FALLBACK_WEBHOOK_ENV)
        return False
    ok = True
    for batch in chunk_embeds(discord_embeds(card)):
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
  h2 { font-size: 17px; margin: 22px 0 10px; border-bottom: 1px solid #1c2944; padding-bottom: 6px; }
  h3 { font-size: 13px; margin: 14px 0 6px; color: #9fb0cc; text-transform: uppercase; letter-spacing: .4px; }
  .toggle { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #9fb0cc; margin: 8px 0 4px; }
  .game { background: #16213a; border: 1px solid #2a3a5c; border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; }
  .card { background: #0d1626; border: 1px solid #2a3a5c; border-radius: 10px; padding: 10px 12px; margin: 8px 0; }
  .card.lowest { border-color: #2ecc71; }
  .title { font-weight: 800; font-size: 15px; }
  .sub { color: #9fb0cc; font-size: 13px; margin-top: 2px; }
  .kv { display: flex; flex-wrap: wrap; gap: 6px 14px; margin-top: 6px; font-size: 13px; }
  .kv b { color: #fff; }
  .legs { margin: 8px 0 0; padding: 0; list-style: none; }
  .legs li { padding: 5px 0; border-top: 1px solid #1c2944; font-size: 13.5px; }
  .legs li .who { font-weight: 700; }
  .shop { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  .book { background: #16213a; border: 1px solid #2a3a5c; border-radius: 8px; padding: 6px 8px; font-size: 12.5px; min-width: 92px; }
  .book.best { border-color: #2ecc71; background: #15351f; }
  .book .abbr { color: #9fb0cc; font-size: 11px; display: block; }
  .book .price { font-weight: 800; }
  .book .miss, .book .dl { color: #9fb0cc; font-size: 11px; white-space: normal; }
  .badge { display: inline-block; padding: 2px 7px; border-radius: 10px; font-size: 11px; font-weight: 700; margin-left: 6px; }
  .badge.stale { background: #3a1818; color: #f87171; }
  .badge.dk { background: #1c2944; color: #7fb3ff; }
  .flag, .note { color: #fbbf24; font-size: 12.5px; }
  .problem { color: #f87171; font-size: 13px; margin-top: 6px; }
  .warn-list { color: #fbbf24; font-size: 13px; margin: 8px 0 0; padding-left: 18px; }
  a { color: #7fb3ff; }
  .btn { display: inline-block; margin-top: 6px; padding: 6px 10px; border-radius: 8px; background: #1f6feb; color: #fff; text-decoration: none; font-size: 13px; font-weight: 700; }
  table.rank { width: 100%; border-collapse: collapse; font-size: 12.5px; margin-top: 6px; }
  table.rank th, table.rank td { padding: 5px 6px; border-bottom: 1px solid #1c2944; text-align: left; white-space: nowrap; }
  table.rank th { color: #9fb0cc; }
  .table-wrap { overflow-x: auto; }
  .compare { display: none; }
  body.show-compare .compare { display: block; }
  .empty { color: #6b7a99; padding: 10px 0; }
  @media (min-width: 900px) { body { font-size: 14px; } }
"""


def _stack_html(v, lowest=False):
    e = html.escape
    legs = []
    for l in v["legs"]:
        flag = f" <span class='flag'>[{e(l['position_flag'])}]</span>" if l.get("position_flag") else ""
        line = f"O{l['line']:g}" if l.get("line") is not None else "O"
        price = f" <span class='sub'>{e(str(l['price']))}</span>" if l.get("price") else ""
        legs.append(f"<li><span class='who'>{e(l['player'])}</span> {e(l['market'])} <b>{line}</b>{price} <span class='sub'>{e(l['team'])} {e(l['slot'])}</span>{flag}</li>")
    corr = f"<span>correlation <b>{v['correlation_ratio']:.2f}x</b> <span class='sub'>(naive {v['naive_decimal']:.1f} / SGP {v['decimal']:.2f})</span></span>" if v.get("correlation_ratio") else "<span>correlation n/a</span>"
    dkc = f"<span>DK corr <b>{v['dk_correlation']:.1f}</b></span>" if v.get("dk_correlation") is not None else ""
    stale = "<span class='badge stale'>STALE</span>" if v["stale"] else ""
    btn = f"<a class='btn' href='{e(v['link'])}' target='_blank' rel='noopener'>Bet at DK {v['american']:+d}</a>" if v.get("link") else "<div class='sub'>no DraftKings deep link returned</div>"
    comp = ""
    if lowest:
        rows = "".join(f"<div class='book{' best' if i == 0 else ''}'><span class='abbr'>{e(c['abbr'])}</span><span class='price'>{c['american']:+d}</span> <span class='sub'>{c['decimal']:.2f}</span><div class='dl'>{c['gap_pct']:+.1f}% vs DK</div>" + (f"<a href='{e(c['link'])}' target='_blank' rel='noopener'>bet</a>" if c.get("link") else "") + "</div>" for i, c in enumerate(v["compare"]))
        skipped = "".join(f"<div class='book'><span class='abbr'>{e(s['abbr'])}</span><span class='miss'>{e(s['why'])}</span>" + (f"<div class='dl'>lines {e('/'.join(f'{x:g}' if x is not None else '?' for x in s['lines']))}</div>" if s.get("lines") and any(x is not None for x in s["lines"]) else "") + "</div>" for s in v["compare_skipped"])
        body = (f"<h3>Other books, same lines as DK (highest payout first)</h3><div class='shop'>{rows}</div>" if rows else "<div class='sub'>no other book priced these exact lines</div>") + (f"<h3>Not compared</h3><div class='shop'>{skipped}</div>" if skipped else "")
        comp = f"<div class='compare'>{body}</div>"
    head = "Lowest-odds stack: " if lowest else ""
    return f"""<div class="card{' lowest' if lowest else ''}"><div class="title">{head}{e(v['name'])} <span class='badge dk'>DK {v['american']:+d} ({v['decimal']:.2f})</span>{stale}</div>
<div class="kv"><span>implied <b>{v['implied'] * 100:.1f}%</b></span>{corr}{dkc}</div>
<ul class="legs">{''.join(legs)}</ul>{btn}{comp}</div>"""


def render_html(card, out_path=PAGE_PATH):
    e = html.escape
    parts = [f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>SGP Play Card</title><style>{CSS}</style></head><body>
<header><h1>The Play Card &middot; SGP stacks</h1><div class="meta">Generated {e(card['generated_at_pt'])} &middot; SGP prices pulled {e(card['sgp']['pulled_at_pt'])} &middot; source: {e(card['source'])} &middot; DraftKings main lines, ranked by lowest odds</div></header>
<div class="subnav"><a href="index.html">MLB Dashboard</a><a href="soccer-dns.html">Soccer DNS</a><a href="sgp.html" class="active">SGP</a></div>
<div id="freshnessBanner" class="freshness-banner {card['overall_tier']}"></div>
<main><label class="toggle"><input type="checkbox" id="compareToggle"> Compare other books for each game's lowest-odds stack (same lines only)</label>"""]
    if card["warnings"]:
        parts.append("<ul class='warn-list'>" + "".join(f"<li>{e(w)}</li>" for w in card["warnings"]) + "</ul>")
    if not card["games"]:
        parts.append("<div class='empty'>No upcoming games priced.</div>")
    for g in card["games"]:
        parts.append(f"<div class='game'><h2>{e(g['away'])} @ {e(g['home'])} <span class='sub'>{e(g['league'].upper())} &middot; kickoff {e(g['kickoff_pt'])} &middot; {g['variants_priced']}/{g['variants_total']} variants priced at DK</span></h2>")
        for n in g["notes"]:
            parts.append(f"<div class='note'>{e(n)}</div>")
        if g["lowest"]:
            parts.append(_stack_html(g["lowest"], lowest=True))
            if len(g["ranked"]) > 1:
                rows = "".join(f"<tr><td>{i + 1}</td><td>{e(v['name'])}</td><td>{v['american']:+d} ({v['decimal']:.2f})</td><td>{v['implied'] * 100:.1f}%</td>"
                               f"<td>{(f'{v['correlation_ratio']:.2f}x') if v.get('correlation_ratio') else '-'}</td><td>{(f'{v['dk_correlation']:.1f}') if v.get('dk_correlation') is not None else '-'}</td>"
                               f"<td>{e(_legs_line(v))}</td></tr>" for i, v in enumerate(g["ranked"]))
                parts.append(f"<h3>All variants at DraftKings, lowest odds first</h3><div class='table-wrap'><table class='rank'><tr><th>#</th><th>Stack</th><th>DK price</th><th>Implied</th><th>Corr</th><th>DK corr</th><th>Legs (DK lines)</th></tr>{rows}</table></div>")
        else:
            parts.append("<div class='empty'>No variant priced at DraftKings.</div>")
        for p in g["dk_problems"]:
            parts.append(f"<div class='problem'>&#9888; DraftKings could not price <b>{e(p['name'])}</b>: {e(p['why'])}</div>")
        parts.append("</div>")
    parts.append(f"""</main><script>
const SGP_AT = {json.dumps(card['sgp']['pulled_at'])}; const FORCED_STALE = {json.dumps(card['overall_tier'] == 'stale' and card['source'] != 'live')};
function fmt(iso) {{ if (!iso) return 'never'; return new Date(iso).toLocaleString('en-US', {{timeZone: 'America/Los_Angeles', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true}}) + ' PT'; }}
function tier(iso, forced) {{ if (forced || !iso) return 'stale'; const m = (Date.now() - new Date(iso)) / 60000; return m <= {FRESH_MINUTES} ? 'fresh' : m <= {WARN_MINUTES} ? 'warn' : 'stale'; }}
function refresh() {{ const s = tier(SGP_AT, FORCED_STALE); const b = document.getElementById('freshnessBanner'); b.className = 'freshness-banner ' + s;
  b.textContent = (s === 'fresh' ? 'FRESH' : s === 'warn' ? 'AGING' : 'STALE') + ' — SGP prices pulled ' + fmt(SGP_AT); }}
refresh(); setInterval(refresh, 60000);
document.getElementById('compareToggle').addEventListener('change', function () {{ document.body.classList.toggle('show-compare', this.checked); }});
</script></body></html>""")
    out = "".join(parts)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    return out_path


# ----- driver ---------------------------------------------------------------------------------------

def run(leagues, price=False, from_saved=False, post=False, limit=None, page_path=PAGE_PATH, card_path=CARD_PATH, event_ids=None):
    """(card, page_path). --price with a missing/rejected key degrades to
    saved prices marked STALE; --from-saved rebuilds from saved prices with
    freshness judged by their real pull time."""
    source, forced_stale, budget = "saved", False, None
    if price and not from_saved:
        try:
            from oddsblaze_client import OddsBlazeClient
            c = OddsBlazeClient()
            for lg in leagues:
                stack_forge.price_slate(c, lg, limit=limit, event_ids=event_ids)
            source, budget = "live", c.guard.summary()
        except Exception as e:
            name = type(e).__name__
            logger.error("live pricing unavailable (%s: %s) - building from last saved prices, marked STALE", name, redact(str(e))[:200])
            source, forced_stale = f"saved ({name})", True
    results = []
    for lg in leagues:
        for r in stack_forge.load_results(league=lg, max_age_days=2):
            r["stacks"] = stack_forge.rank_stacks(r["stacks"])  # re-rank saved files under the current rules
            results.append(r)
    card = build_card(leagues, results=results, forced_stale=forced_stale, budget=budget, source=source)
    save_card(card, card_path)
    render_html(card, page_path)
    if post:
        post_discord(card)
    return card, page_path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league", default="nfl,ncaaf")
    ap.add_argument("--price", action="store_true", help="price stacks live via OddsBlaze (needs ODDSBLAZE_API_KEY)")
    ap.add_argument("--from-saved", action="store_true", help="never call OddsBlaze; rebuild from saved prices")
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
    print(f"card: {CARD_PATH}\npage: {page}\ntier: {card['overall_tier']} | games: {len(card['games'])} | warnings: {card['warnings']}")


if __name__ == "__main__":
    main()
