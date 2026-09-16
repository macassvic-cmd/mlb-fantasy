"""
Stack Forge - the 6-man SGP stack pricer behind the Play Card (2026-09-16).

BASE STACK per game (6 legs, one same-game parlay):
    away: QB1 passing yds OVER + WR1 receiving yds OVER + WR2 receiving yds OVER
    home: same
VARIANTS (one slot swapped, per team, still six legs):
    WR2->TE1, WR2->WR3, WR2->RB1 (receiving yds), WR1->TE1

ROSTER / RANKING from DraftKings' feed (OddsBlaze supplies player.position):
    QB1 = the team's QB with a passing-yards prop, highest main line
    WR1..WR3 / TE1 = that position's players ranked by receiving-yards main line
    RB1 = RBs ranked by rushing-yards main line
A player with no position is inferred (passing prop -> QB, rushing line >
receiving line -> RB, else WR) and the guess is flagged on every output.

MAIN LINE per book: books differ - DraftKings flags one side of the main
line, Fanatics flags every ladder rung, Hard Rock/BetMGM flag nothing. So:
candidates = flagged lines if any else all; prefer a line offered on BOTH
sides; then nearest to DraftKings' main for the SAME market; then closest
to even money. Anything but a single clean flag is noted next to the line.
Every stack row always shows each book's line next to its price.

PRICING: one SGP call per (stack, book) at every book that has all six
legs; a book missing a leg is listed with the missing legs. Calls go
through oddsblaze_client (spacing, per-minute caps, per-day cap, 429
backoff, raw saves). BUDGET: before pricing, the planner counts the SGP
calls a game needs; if that exceeds the guard's remaining daily budget it
drops variants in a fixed order (WR2->RB1 first, then WR1->TE1, WR2->WR3,
WR2->TE1; Base is never dropped) and records what was trimmed so the card
can say so.

RANKING: stacks ranked by best decimal price; runner-up book and spread %
= (best - runner_up) / runner_up. Results persist to
data/oddsblaze/playcard/stacks/<league>_<event>.json (committed, small),
which is also what the card renders from when no key is available.
"""

import json
import logging
import os
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from oddsblaze_client import AccessBlocked, BudgetExhausted, InvalidResponse, OddsBlazeClient

logger = logging.getLogger("stack_forge")

STACKS_DIR = os.path.join("data", "oddsblaze", "playcard", "stacks")

BOOKS = ["draftkings", "caesars", "betmgm", "fanatics", "hard-rock", "betrivers", "betparx", "bally-bet"]
BOOK_ABBR = {"draftkings": "DK", "caesars": "CZR", "betmgm": "MGM", "fanatics": "FAN", "hard-rock": "HR", "betrivers": "BR", "betparx": "PARX", "bally-bet": "BALLY"}
MARKET_NAMES = {
    "player-passing-yards": "Player Passing Yards",
    "player-receiving-yards": "Player Receiving Yards",
    "player-rushing-yards": "Player Rushing Yards",
    "player-receptions": "Player Receptions",
    "player-touchdowns": "Player Touchdowns",
}
MARKET_SHORT = {"Player Passing Yards": "pass yds", "Player Receiving Yards": "rec yds", "Player Rushing Yards": "rush yds", "Player Receptions": "rec", "Player Touchdowns": "TD"}
SLOT_DEFAULTS = {"QB1": "player-passing-yards", "WR1": "player-receiving-yards", "WR2": "player-receiving-yards", "WR3": "player-receiving-yards", "TE1": "player-receiving-yards", "RB1": "player-receiving-yards"}
SIDE = "Over"
# Variant drop order when the budget is short (first dropped first). Base is never dropped.
VARIANT_PRIORITY_DROP = [("WR2", "RB1"), ("WR1", "TE1"), ("WR2", "WR3"), ("WR2", "TE1")]
VARIANTS = [("WR2", "TE1"), ("WR2", "WR3"), ("WR2", "RB1"), ("WR1", "TE1")]


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z0-9]", "", "".join(ch for ch in s if not unicodedata.combining(ch)).lower())


def american_from_decimal(d):
    return round((d - 1) * 100) if d >= 2 else round(-100 / (d - 1))


def to_float(p):
    try:
        return float(str(p).replace("+", ""))
    except (TypeError, ValueError):
        return None


# ----- per-book line index ------------------------------------------------------

class BookLines:
    """(player_norm, market) -> {line: {side: odd}} for one book + event."""

    def __init__(self, event):
        self.by_pm = defaultdict(lambda: defaultdict(dict))
        for o in (event or {}).get("odds", []):
            sel = o.get("selection") or {}
            p = norm((o.get("player") or {}).get("name") or sel.get("name"))
            if sel.get("line") is None or not sel.get("side"):
                continue
            self.by_pm[(p, o.get("market"))][sel["line"]][sel["side"]] = o

    def main_line(self, player, market, dk_line=None):
        lines = self.by_pm.get((player, market))
        if not lines:
            return None, "no prop"
        flagged = [l for l, sides in lines.items() if any(o.get("main") for o in sides.values())]
        if len(flagged) == 1:
            return flagged[0], None
        cands = flagged or list(lines)
        note = "several main-flagged lines" if flagged else "no main flag"
        two_sided = [l for l in cands if len(lines[l]) >= 2]
        if two_sided:
            cands = two_sided
            note += " - two-sided line used"
        if len(cands) > 1 and dk_line is not None:
            cands = [min(cands, key=lambda l: abs(l - dk_line))]
            note += " (nearest to DK)"
        elif len(cands) > 1:
            cands = [min(cands, key=lambda l: min(abs(to_float(o.get("price")) or 9999) for o in lines[l].values()))]
            note += " (closest to even money)"
        return cands[0], note

    def resolve(self, player, market, side, dk_line=None):
        line, note = self.main_line(player, market, dk_line)
        if line is None:
            return None, note
        o = self.by_pm[(player, market)][line].get(side)
        if o is None:
            return None, f"{side} not offered at main line {line:g}"
        return o, note


# ----- roster + stacks ------------------------------------------------------------

def build_roster(dk_event):
    lines = BookLines(dk_event)
    players = {}
    for o in dk_event.get("odds", []):
        pl = o.get("player") or {}
        if not pl.get("name"):
            continue
        n = norm(pl["name"])
        players.setdefault(n, {"name": pl["name"], "norm": n, "position": pl.get("position"), "team": (pl.get("team") or {}).get("abbreviation")})

    def main(n, market_id):
        return lines.main_line(n, MARKET_NAMES[market_id])[0]

    for info in players.values():
        info["position_flag"] = None
        if not info["position"]:
            if main(info["norm"], "player-passing-yards") is not None:
                info["position"], info["position_flag"] = "QB", "position inferred from passing-yards prop"
            elif (main(info["norm"], "player-rushing-yards") or 0) > (main(info["norm"], "player-receiving-yards") or 0):
                info["position"], info["position_flag"] = "RB", "position inferred (rushing line > receiving line)"
            else:
                info["position"], info["position_flag"] = "WR", "position inferred (receiving prop, no rushing edge)"
    roster = {}
    for team in {p["team"] for p in players.values() if p["team"]}:
        tp = [p for p in players.values() if p["team"] == team]

        def ranked(pos, market_id):
            rows = [(main(p["norm"], market_id), p) for p in tp if p["position"] == pos]
            rows = [(l, p) for l, p in rows if l is not None]
            rows.sort(key=lambda t: -t[0])
            return [dict(p, dk_line=l) for l, p in rows]

        qbs, wrs, tes, rbs = ranked("QB", "player-passing-yards"), ranked("WR", "player-receiving-yards"), ranked("TE", "player-receiving-yards"), ranked("RB", "player-rushing-yards")
        roster[team] = {"QB1": qbs[0] if qbs else None, "WR1": wrs[0] if len(wrs) > 0 else None, "WR2": wrs[1] if len(wrs) > 1 else None,
                        "WR3": wrs[2] if len(wrs) > 2 else None, "TE1": tes[0] if tes else None, "RB1": rbs[0] if rbs else None}
    return roster


def build_stacks(roster, away, home, slot_markets=None, dk_lines=None):
    """[{"name", "variant": (out, in) | None, "team", "legs": [...]}], note."""
    slot_markets = dict(SLOT_DEFAULTS, **(slot_markets or {}))

    def leg(team, slot):
        p = roster.get(team, {}).get(slot)
        if not p:
            return None
        market = MARKET_NAMES[slot_markets[slot]]
        dk_line = dk_lines.main_line(p["norm"], market)[0] if dk_lines else p.get("dk_line")
        return {"team": team, "slot": slot, "player": p["name"], "player_norm": p["norm"], "market": market, "side": SIDE,
                "dk_line": dk_line, "position": p["position"], "position_flag": p.get("position_flag")}

    base_slots = [(away, "QB1"), (away, "WR1"), (away, "WR2"), (home, "QB1"), (home, "WR1"), (home, "WR2")]
    base = [leg(t, s) for t, s in base_slots]
    if any(l is None for l in base):
        missing = ", ".join(f"{t} {s}" for (t, s), l in zip(base_slots, base) if l is None)
        return [], f"base stack not buildable at DraftKings: missing {missing}"
    stacks = [{"name": "Base", "variant": None, "team": None, "legs": base}]
    skipped = []
    for team in (away, home):
        for out_slot, in_slot in VARIANTS:
            repl = leg(team, in_slot)
            if repl is None:
                skipped.append(f"{team} {out_slot}→{in_slot}: no {in_slot} at DraftKings")
                continue
            legs = [repl if (l["team"] == team and l["slot"] == out_slot) else l for l in base]
            stacks.append({"name": f"{team}: {out_slot}→{in_slot}", "variant": (out_slot, in_slot), "team": team, "legs": legs})
    return stacks, ("; ".join(skipped) if skipped else None)


# ----- budget-aware planning -------------------------------------------------------

def plan_calls(stacks, book_lines, books):
    """{stack name: [books that have all six legs]} - the SGP calls a game needs."""
    plan = {}
    for stack in stacks:
        priced_books = []
        for book in books:
            bl = book_lines[book]
            if all(bl.resolve(l["player_norm"], l["market"], l["side"], l["dk_line"])[0] is not None for l in stack["legs"]):
                priced_books.append(book)
        plan[stack["name"]] = priced_books
    return plan


def trim_to_budget(stacks, plan, remaining):
    """Drop whole variants (in VARIANT_PRIORITY_DROP order, both teams) until
    the planned SGP calls fit `remaining`. Returns (kept stacks, trimmed note)."""
    kept = list(stacks)
    needed = sum(len(plan.get(s["name"], [])) for s in kept)
    dropped = []
    for variant in VARIANT_PRIORITY_DROP:
        if needed <= remaining:
            break
        before = len(kept)
        kept = [s for s in kept if s["variant"] != variant]
        if len(kept) != before:
            dropped.append(f"{variant[0]}→{variant[1]}")
            needed = sum(len(plan.get(s["name"], [])) for s in kept)
    note = None
    if dropped:
        note = f"budget guard: dropped variant(s) {', '.join(dropped)} (needed more SGP calls than the remaining daily budget of {remaining})"
    if needed > remaining:
        # Even Base doesn't fit: price Base at as many books as allowed.
        kept = [s for s in kept if s["variant"] is None]
        note = (note or "budget guard:") + f" - only the Base stack at up to {remaining} book(s)"
    return kept, note


# ----- pricing --------------------------------------------------------------------

def describe(o):
    sel = o.get("selection") or {}
    return {"market": o.get("market"), "name": o.get("name"), "price": o.get("price"), "main": o.get("main"), "line": sel.get("line"), "side": sel.get("side"), "updated": o.get("updated")}


def price_stack_at_book(client, book, stack, bl):
    resolved, missing, notes = [], [], []
    for leg in stack["legs"]:
        o, note = bl.resolve(leg["player_norm"], leg["market"], leg["side"], leg["dk_line"])
        if o is None:
            missing.append(f'{leg["player"]} {MARKET_SHORT.get(leg["market"], leg["market"])} ({note})')
            resolved.append(None)
        else:
            resolved.append({"line": o["selection"]["line"], "price": o.get("price"), "note": note, "sgp": o.get("sgp")})
            if note:
                notes.append(f'{leg["player"]}: {note}')
    out = {"book": book, "legs": [None if r is None else {k: v for k, v in r.items() if k != "sgp"} for r in resolved], "missing": missing, "notes": notes, "priced": False}
    if missing:
        return out
    try:
        st, body = client.sgp(book, [r["sgp"] for r in resolved])
    except BudgetExhausted as e:
        out["error"] = f"budget: {e}"
        return out
    except (AccessBlocked, InvalidResponse) as e:
        out["error"] = str(e)[:160]
        return out
    out["sgp_at"] = datetime.now(timezone.utc).isoformat()
    out["http_status"] = st
    dec = to_float(body.get("price")) if isinstance(body, dict) else None
    if dec:
        out.update({"priced": True, "decimal": dec, "american": american_from_decimal(dec), "implied": round(1 / dec, 4),
                    "link": (body.get("links") or {}).get("desktop"), "correlation": body.get("correlation")})
        if isinstance(body, dict) and body.get("_replayed_from"):
            out["replayed_from"] = body["_replayed_from"]
    else:
        out["error"] = (body.get("message") if isinstance(body, dict) else str(body))[:160]
    return out


def rank_stacks(stacks_out):
    """Adds best_book / runner_up / spread_pct per stack, returns stacks sorted best-first."""
    for s in stacks_out:
        priced = sorted([b for b in s["books"] if b.get("priced")], key=lambda b: -b["decimal"])
        s["best_book"] = priced[0]["book"] if priced else None
        s["best_decimal"] = priced[0]["decimal"] if priced else None
        s["best_american"] = priced[0]["american"] if priced else None
        s["best_link"] = priced[0].get("link") if priced else None
        s["runner_up_book"] = priced[1]["book"] if len(priced) > 1 else None
        s["runner_up_decimal"] = priced[1]["decimal"] if len(priced) > 1 else None
        s["spread_pct"] = round((priced[0]["decimal"] - priced[1]["decimal"]) / priced[1]["decimal"] * 100, 1) if len(priced) > 1 else None
        s["books_priced"] = len(priced)
    return sorted(stacks_out, key=lambda s: -(s["best_decimal"] or 0))


def pull_league_odds(client, league, books):
    out = {}
    for book in books:
        try:
            st, body = client.odds(book, league, market=",".join(MARKET_NAMES), tag=f"odds_{book}_{league}_props")
        except (InvalidResponse, BudgetExhausted) as e:
            logger.warning("odds %s %s failed: %s", book, league, e)
            st, body = None, {}
        events = {e["id"]: e for e in (body.get("events") or [])} if isinstance(body, dict) and st == 200 else {}
        out[book] = {"events": events, "feed_updated": body.get("updated") if isinstance(body, dict) else None,
                     "pulled_at": datetime.now(timezone.utc).isoformat(), "status": st, "replayed_from": body.get("_replayed_from") if isinstance(body, dict) else None}
        logger.info("odds %-10s %s: HTTP %s, %d events", book, league, st, len(events))
    return out


def price_event(client, league, event_meta, league_odds, books=None, slot_markets=None, remaining_sgp=None):
    books = books or BOOKS
    event_id = event_meta["id"]
    away, home = event_meta["teams"]["away"]["abbreviation"], event_meta["teams"]["home"]["abbreviation"]
    result = {"league": league, "event": event_id, "away": away, "home": home, "kickoff": event_meta["date"], "slot_markets": dict(SLOT_DEFAULTS, **(slot_markets or {})),
              "odds_pulled": {b: {k: v for k, v in league_odds[b].items() if k != "events"} | {"has_event": event_id in league_odds[b]["events"]} for b in books},
              "priced_at": datetime.now(timezone.utc).isoformat(), "stacks": [], "roster": {}, "notes": []}
    dk_event = league_odds.get("draftkings", {}).get("events", {}).get(event_id)
    if not dk_event:
        result["notes"].append("DraftKings has no props for this game yet")
        return result
    roster = build_roster(dk_event)
    result["roster"] = roster
    book_lines = {b: BookLines(league_odds[b]["events"].get(event_id)) for b in books}
    stacks, note = build_stacks(roster, away, home, slot_markets, book_lines["draftkings"])
    if note:
        result["notes"].append(note)
    plan = plan_calls(stacks, book_lines, books)
    remaining = client.guard.remaining("sgp") if remaining_sgp is None else remaining_sgp
    stacks, trim_note = trim_to_budget(stacks, plan, remaining)
    if trim_note:
        result["notes"].append(trim_note)
        result["budget_trimmed"] = True
    for stack in stacks:
        entry = {"name": stack["name"], "variant": list(stack["variant"]) if stack["variant"] else None, "legs": stack["legs"], "books": []}
        for book in books:
            b = price_stack_at_book(client, book, stack, book_lines[book])
            entry["books"].append(b)
            logger.info("%s@%s %-14s %-11s -> %s", away, home, stack["name"], book,
                        f'{b["decimal"]:.2f} ({b["american"]:+d})' if b["priced"] else (("missing: " + "; ".join(b["missing"]))[:100] if b["missing"] else f'error: {b.get("error")}'))
        result["stacks"].append(entry)
    result["stacks"] = rank_stacks(result["stacks"])
    best = next((s for s in result["stacks"] if s["best_book"]), None)
    if best:
        result["best_overall"] = {"stack": best["name"], "book": best["best_book"], "decimal": best["best_decimal"], "american": best["best_american"], "link": best.get("best_link")}
    return result


# ----- persistence ------------------------------------------------------------------

def result_path(league, event_id, stacks_dir=STACKS_DIR):
    return os.path.join(stacks_dir, f"{league}_{event_id}.json")


def save_result(result, stacks_dir=STACKS_DIR):
    os.makedirs(stacks_dir, exist_ok=True)
    path = result_path(result["league"], result["event"], stacks_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_results(stacks_dir=STACKS_DIR, league=None, max_age_days=None):
    out = []
    if not os.path.isdir(stacks_dir):
        return out
    for f in sorted(os.listdir(stacks_dir)):
        if not f.endswith(".json") or (league and not f.startswith(league + "_")):
            continue
        try:
            with open(os.path.join(stacks_dir, f), encoding="utf-8") as fh:
                r = json.load(fh)
        except Exception:
            continue
        if max_age_days and r.get("kickoff"):
            try:
                ko = datetime.fromisoformat(r["kickoff"].replace("Z", "+00:00"))
                if ko < datetime.now(timezone.utc) - timedelta(days=max_age_days):
                    continue
            except ValueError:
                pass
        out.append(r)
    return sorted(out, key=lambda r: (r.get("kickoff") or "", r.get("away") or ""))


# ----- slate driver ------------------------------------------------------------------

def upcoming_events(client, league, days=7):
    st, sched = client.schedule(league)
    now = datetime.now(timezone.utc)
    events = []
    for e in (sched.get("events") or []) if isinstance(sched, dict) else []:
        try:
            ko = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        except (KeyError, ValueError, AttributeError):
            continue
        if now - timedelta(hours=6) <= ko <= now + timedelta(days=days):
            events.append(e)
    return sorted(events, key=lambda e: e["date"])


def price_slate(client, league, books=None, limit=None, event_ids=None, skip_priced=False, days=7, stacks_dir=STACKS_DIR, on_game_done=None):
    """Price every eligible game (base stack buildable at DraftKings) in the
    league's upcoming window. Returns the list of results written."""
    books = books or BOOKS
    events = upcoming_events(client, league, days)
    if event_ids:
        events = [e for e in events if e["id"] in set(event_ids)]
    if skip_priced:
        events = [e for e in events if not os.path.exists(result_path(league, e["id"], stacks_dir))]
    league_odds = pull_league_odds(client, league, books)
    eligible = []
    for e in events:
        dk_event = league_odds["draftkings"]["events"].get(e["id"])
        if not dk_event:
            continue
        stacks, _ = build_stacks(build_roster(dk_event), e["teams"]["away"]["abbreviation"], e["teams"]["home"]["abbreviation"], None, BookLines(dk_event))
        if stacks:
            eligible.append(e)
    if limit:
        eligible = eligible[:limit]
    logger.info("%s: %d eligible game(s): %s", league, len(eligible), [f'{e["teams"]["away"]["abbreviation"]}@{e["teams"]["home"]["abbreviation"]}' for e in eligible])
    results = []
    for e in eligible:
        r = price_event(client, league, e, league_odds, books)
        save_result(r, stacks_dir)
        results.append(r)
        if on_game_done:
            on_game_done(r)
    return results
