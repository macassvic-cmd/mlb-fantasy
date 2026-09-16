"""Unit tests for stack_forge.py - roster ranking, variant generation,
per-book main-line selection, ranking/spread, and the budget guard's
variant trimming. No network: pricing uses a fake client.

Run with: python -m unittest test_stack_forge -v
"""

import json
import os
import shutil
import tempfile
import unittest

import stack_forge as sf


def _odd(player, team, position, market, line, side, price, main=False, sgp=None):
    return {"id": f"DraftKings#ev#{market}#{player} {side} {line}", "market": market, "name": f"{player} {side} {line}", "price": price, "main": main,
            "sgp": sgp or f"tok-{player}-{market}-{side}-{line}", "selection": {"name": player, "side": side, "line": line},
            "player": {"id": player.lower(), "name": player, "position": position, "team": {"abbreviation": team}}}


def _two_sided(player, team, pos, market, line, main_side="Under"):
    return [_odd(player, team, pos, market, line, "Over", "-110", main=(main_side == "Over")),
            _odd(player, team, pos, market, line, "Under", "-110", main=(main_side == "Under"))]


PASS, REC, RUSH = "Player Passing Yards", "Player Receiving Yards", "Player Rushing Yards"


def dk_event(with_te=True, with_wr3=True, with_rb=True, unknown_position=False):
    odds = []
    odds += _two_sided("Jared Goff", "DET", "QB", PASS, 270.5)
    odds += _two_sided("Amon-Ra St. Brown", "DET", "WR", REC, 82.5)
    odds += _two_sided("Jameson Williams", "DET", "WR", REC, 59.5)
    if with_wr3:
        odds += _two_sided("Isaac TeSlaa", "DET", "WR", REC, 14.5)
    if with_te:
        odds += _two_sided("Sam LaPorta", "DET", "TE", REC, 45.5)
    if with_rb:
        odds += _two_sided("Jahmyr Gibbs", "DET", "RB", RUSH, 90.5)
        odds += _two_sided("Jahmyr Gibbs", "DET", "RB", REC, 30.5)
        # DK-style ladder: Over-only alternates, not flagged main
        for l in (49.5, 79.5, 99.5):
            odds.append(_odd("Jahmyr Gibbs", "DET", "RB", REC, l, "Over", "+900"))
    odds += _two_sided("Josh Allen", "BUF", "QB", PASS, 251.5)
    odds += _two_sided("Khalil Shakir", "BUF", "WR", REC, 45.5)
    odds += _two_sided("Joshua Palmer", "BUF", "WR", REC, 13.5)
    odds += _two_sided("Keon Coleman", "BUF", "WR", REC, 12.5)
    odds += _two_sided("Dalton Kincaid", "BUF", "TE", REC, 50.5)
    odds += _two_sided("James Cook III", "BUF", "RB", RUSH, 78.5)
    if unknown_position:
        odds += _two_sided("Mystery Man", "BUF", None, REC, 20.5)
        odds += _two_sided("Mystery Back", "BUF", None, RUSH, 40.5)
        odds += _two_sided("Mystery Back", "BUF", None, REC, 10.5)
    return {"id": "ev", "teams": {"away": {"abbreviation": "DET"}, "home": {"abbreviation": "BUF"}}, "date": "2026-09-18T00:15:00.000Z", "odds": odds}


class TestRoster(unittest.TestCase):
    def test_ranking_by_position_and_main_line(self):
        r = sf.build_roster(dk_event())
        det = r["DET"]
        self.assertEqual(det["QB1"]["name"], "Jared Goff")
        self.assertEqual([det["WR1"]["name"], det["WR2"]["name"], det["WR3"]["name"]], ["Amon-Ra St. Brown", "Jameson Williams", "Isaac TeSlaa"])
        self.assertEqual(det["TE1"]["name"], "Sam LaPorta")
        self.assertEqual(det["RB1"]["name"], "Jahmyr Gibbs")
        self.assertEqual(det["RB1"]["dk_line"], 90.5)  # ranked by RUSHING line
        self.assertEqual(r["BUF"]["WR3"]["name"], "Keon Coleman")
        self.assertIsNone(det["QB1"]["position_flag"])

    def test_unknown_position_is_inferred_and_flagged(self):
        r = sf.build_roster(dk_event(unknown_position=True))
        buf = r["BUF"]
        names = {slot: (p["name"], p.get("position_flag")) for slot, p in buf.items() if p}
        self.assertEqual(names["WR2"][0], "Mystery Man")  # 20.5 ranks between Shakir 45.5 and Palmer 13.5
        self.assertIn("inferred", names["WR2"][1])
        self.assertEqual(names["WR3"][0], "Joshua Palmer")
        self.assertEqual(buf["RB1"]["name"], "James Cook III")  # real RB outranks the inferred one at 40.5
        flagged = [p for p in buf.values() if p and p.get("position_flag")]
        self.assertTrue(all("inferred" in p["position_flag"] for p in flagged))


class TestStacks(unittest.TestCase):
    def test_base_and_eight_variants(self):
        ev = dk_event()
        stacks, note = sf.build_stacks(sf.build_roster(ev), "DET", "BUF", None, sf.BookLines(ev))
        self.assertIsNone(note)
        self.assertEqual([s["name"] for s in stacks], ["Base", "DET: WR2→TE1", "DET: WR2→WR3", "DET: WR2→RB1", "DET: WR1→TE1",
                                                          "BUF: WR2→TE1", "BUF: WR2→WR3", "BUF: WR2→RB1", "BUF: WR1→TE1"])
        base = stacks[0]
        self.assertEqual([(l["team"], l["slot"], l["market"]) for l in base["legs"]],
                         [("DET", "QB1", PASS), ("DET", "WR1", REC), ("DET", "WR2", REC), ("BUF", "QB1", PASS), ("BUF", "WR1", REC), ("BUF", "WR2", REC)])
        self.assertTrue(all(l["side"] == "Over" for l in base["legs"]))
        rb_variant = next(s for s in stacks if s["name"] == "DET: WR2→RB1")
        rb_leg = rb_variant["legs"][2]
        self.assertEqual((rb_leg["player"], rb_leg["market"], rb_leg["slot"]), ("Jahmyr Gibbs", REC, "RB1"))
        self.assertEqual(rb_leg["dk_line"], 30.5)  # the leg's OWN market main line, not the rushing line it was ranked by
        self.assertEqual(len({s["name"] for s in stacks}), len(stacks))
        self.assertTrue(all(len(s["legs"]) == 6 for s in stacks))

    def test_missing_slot_skips_variant_with_note(self):
        ev = dk_event(with_te=False)
        stacks, note = sf.build_stacks(sf.build_roster(ev), "DET", "BUF", None, sf.BookLines(ev))
        self.assertNotIn("DET: WR2→TE1", [s["name"] for s in stacks])
        self.assertIn("DET WR2→TE1: no TE1", note)
        self.assertIn("BUF: WR2→TE1", [s["name"] for s in stacks])

    def test_base_not_buildable_reports_missing(self):
        ev = dk_event()
        ev["odds"] = [o for o in ev["odds"] if o["player"]["name"] not in ("Joshua Palmer", "Keon Coleman")]  # BUF left with one WR
        stacks, note = sf.build_stacks(sf.build_roster(ev), "DET", "BUF")
        self.assertEqual(stacks, [])
        self.assertIn("BUF WR2", note)

    def test_slot_market_is_configurable(self):
        ev = dk_event()
        ev["odds"] += _two_sided("Jameson Williams", "DET", "WR", "Player Receptions", 3.5)
        stacks, _ = sf.build_stacks(sf.build_roster(ev), "DET", "BUF", {"WR2": "player-receptions"}, sf.BookLines(ev))
        self.assertEqual(stacks[0]["legs"][2]["market"], "Player Receptions")
        self.assertEqual(stacks[0]["legs"][2]["dk_line"], 3.5)


class TestMainLine(unittest.TestCase):
    def test_single_flag_is_the_main_line_and_over_side_is_used(self):
        bl = sf.BookLines(dk_event())
        o, note = bl.resolve(sf.norm("Jahmyr Gibbs"), REC, "Over", 30.5)
        self.assertEqual(o["selection"]["line"], 30.5)
        self.assertIsNone(note)

    def test_fanatics_style_every_rung_flagged_prefers_two_sided_then_nearest_dk(self):
        ev = {"odds": [_odd("Jahmyr Gibbs", "DET", "RB", REC, l, "Over", "+300", main=True) for l in (9.5, 19.5, 29.5, 79.5)]}
        bl = sf.BookLines(ev)
        line, note = bl.main_line(sf.norm("Jahmyr Gibbs"), REC, dk_line=30.5)
        self.assertEqual(line, 29.5)
        self.assertIn("nearest to DK", note)
        ev["odds"] += [_odd("Jahmyr Gibbs", "DET", "RB", REC, 29.5, "Under", "-120", main=True)]
        line, note = sf.BookLines(ev).main_line(sf.norm("Jahmyr Gibbs"), REC, dk_line=90.5)
        self.assertEqual(line, 29.5)  # two-sided beats "nearest" even with a wrong reference
        self.assertIn("two-sided", note)

    def test_no_flags_falls_back_to_nearest_dk_or_even_money(self):
        ev = {"odds": [_odd("Jahmyr Gibbs", "DET", "RB", REC, l, "Over", p) for l, p in ((24.5, "-180"), (39.5, "+175"), (79.5, "+2000"))]}
        bl = sf.BookLines(ev)
        self.assertEqual(bl.main_line(sf.norm("Jahmyr Gibbs"), REC, dk_line=30.5)[0], 24.5)
        line, note = bl.main_line(sf.norm("Jahmyr Gibbs"), REC, dk_line=None)
        self.assertEqual(line, 39.5)
        self.assertIn("even money", note)

    def test_missing_side_or_prop_reports_why(self):
        ev = {"odds": [_odd("Jahmyr Gibbs", "DET", "RB", REC, 30.5, "Under", "-110", main=True)]}
        o, note = sf.BookLines(ev).resolve(sf.norm("Jahmyr Gibbs"), REC, "Over", 30.5)
        self.assertIsNone(o)
        self.assertIn("Over not offered", note)
        self.assertEqual(sf.BookLines(ev).resolve("nobody", REC, "Over")[1], "no prop")


class FakeClient:
    """Prices every stack at a fixed decimal per book; records calls."""

    def __init__(self, prices, remaining=1000):
        self.prices = prices
        self.calls = []
        self.guard = type("G", (), {"remaining": staticmethod(lambda klass: remaining), "summary": staticmethod(lambda: {})})()

    def sgp(self, book, tokens, price="decimal", tag=None):
        self.calls.append((book, tuple(tokens)))
        p = self.prices.get(book)
        if p is None:
            return 400, {"message": "Price not found"}
        return 200, {"price": f"{p:.2f}", "links": {"desktop": f"https://{book}.test/slip"}}


def _league_odds(books, drop_leg_at=None):
    ev = dk_event()
    out = {}
    for b in books:
        e = json.loads(json.dumps(ev))
        if drop_leg_at and b in drop_leg_at:
            e["odds"] = [o for o in e["odds"] if o["player"]["name"] != drop_leg_at[b]]
        out[b] = {"events": {"ev": e}, "feed_updated": "2026-09-16T07:00:00Z", "pulled_at": "2026-09-16T07:00:01+00:00", "status": 200, "replayed_from": None}
    return out


EVENT_META = {"id": "ev", "date": "2026-09-18T00:15:00.000Z", "teams": {"away": {"abbreviation": "DET"}, "home": {"abbreviation": "BUF"}}}


class TestPricingAndRanking(unittest.TestCase):
    def test_prices_every_stack_at_every_book_with_all_legs_and_ranks(self):
        books = ["draftkings", "caesars", "betmgm"]
        client = FakeClient({"draftkings": 19.0, "caesars": 18.5, "betmgm": 13.0})
        r = sf.price_event(client, "nfl", EVENT_META, _league_odds(books, drop_leg_at={"betmgm": "Joshua Palmer"}), books)
        self.assertEqual(len(r["stacks"]), 9)
        base = next(s for s in r["stacks"] if s["name"] == "Base")
        self.assertEqual(base["best_book"], "draftkings")
        self.assertEqual(base["best_american"], 1800)
        self.assertEqual(base["runner_up_book"], "caesars")
        self.assertAlmostEqual(base["spread_pct"], round((19.0 - 18.5) / 18.5 * 100, 1))
        mgm = next(b for b in base["books"] if b["book"] == "betmgm")
        self.assertFalse(mgm["priced"])
        self.assertIn("Joshua Palmer rec yds", mgm["missing"][0])
        # every priced book row carries its own line per leg
        dk = next(b for b in base["books"] if b["book"] == "draftkings")
        self.assertEqual([l["line"] for l in dk["legs"]], [270.5, 82.5, 59.5, 251.5, 45.5, 13.5])
        self.assertEqual(base["best_link"], "https://draftkings.test/slip")
        # ranked best-first
        decs = [s["best_decimal"] or 0 for s in r["stacks"]]
        self.assertEqual(decs, sorted(decs, reverse=True))
        self.assertEqual(r["best_overall"]["book"], "draftkings")
        # BUF: WR2->RB1 needs James Cook receiving yards, which no book has -> not priced anywhere, no SGP call for it
        cook = next(s for s in r["stacks"] if s["name"] == "BUF: WR2→RB1")
        self.assertIsNone(cook["best_book"])
        self.assertTrue(all("James Cook III rec yds" in b["missing"][0] for b in cook["books"]))
        # DK + CZR price the 8 stacks that don't need Cook's receiving yards; MGM (no Palmer)
        # can only price the two BUF variants that swap Palmer out for TE1 / WR3 -> 8 + 8 + 2
        self.assertEqual(len(client.calls), 18)
        self.assertTrue(all(not any(t is None for t in toks) for _, toks in client.calls))

    def test_price_not_found_is_an_error_not_a_price(self):
        client = FakeClient({"draftkings": 19.0})  # caesars -> 400
        r = sf.price_event(client, "nfl", EVENT_META, _league_odds(["draftkings", "caesars"]), ["draftkings", "caesars"])
        czr = next(b for b in r["stacks"][0]["books"] if b["book"] == "caesars")
        self.assertFalse(czr["priced"])
        self.assertEqual(czr["error"], "Price not found")

    def test_budget_trims_variants_in_priority_order_and_notes_it(self):
        books = ["draftkings", "caesars"]
        client = FakeClient({"draftkings": 19.0, "caesars": 18.5}, remaining=6)  # 9 stacks x 2 books = 18 needed
        r = sf.price_event(client, "nfl", EVENT_META, _league_odds(books), books)
        names = [s["name"] for s in r["stacks"]]
        self.assertIn("Base", names)
        self.assertTrue(r["budget_trimmed"])
        self.assertTrue(any("budget guard" in n for n in r["notes"]))
        self.assertNotIn("DET: WR2→RB1", names)  # dropped first
        self.assertLessEqual(len(client.calls), 6)

    def test_trim_keeps_base_when_even_base_does_not_fit(self):
        stacks = [{"name": "Base", "variant": None}, {"name": "v", "variant": ("WR2", "TE1")}]
        plan = {"Base": ["a", "b", "c"], "v": ["a", "b"]}
        kept, note = sf.trim_to_budget(stacks, plan, remaining=2)
        self.assertEqual([s["name"] for s in kept], ["Base"])
        self.assertIn("only the Base stack", note)

    def test_no_trim_when_budget_is_enough(self):
        stacks = [{"name": "Base", "variant": None}, {"name": "v", "variant": ("WR2", "TE1")}]
        kept, note = sf.trim_to_budget(stacks, {"Base": ["a"], "v": ["a"]}, remaining=5)
        self.assertEqual(len(kept), 2)
        self.assertIsNone(note)


class TestPersistence(unittest.TestCase):
    def test_save_and_load_results_sorted_by_kickoff(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        r1 = {"league": "nfl", "event": "b", "kickoff": "2026-09-21T00:20:00Z", "away": "IND", "home": "KC", "stacks": []}
        r2 = {"league": "nfl", "event": "a", "kickoff": "2026-09-18T00:15:00Z", "away": "DET", "home": "BUF", "stacks": []}
        sf.save_result(r1, d)
        sf.save_result(r2, d)
        loaded = sf.load_results(d, league="nfl")
        self.assertEqual([r["event"] for r in loaded], ["a", "b"])
        self.assertEqual(sf.load_results(d, league="ncaaf"), [])
        self.assertFalse(any(f.endswith(".tmp") for f in os.listdir(d)))


if __name__ == "__main__":
    unittest.main()
