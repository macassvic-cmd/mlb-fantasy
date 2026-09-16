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
    def test_prices_every_variant_at_draftkings_only_then_compares_lowest_at_same_lines(self):
        books = ["draftkings", "caesars", "betmgm", "hard-rock"]
        lo = _league_odds(books, drop_leg_at={"betmgm": "Joshua Palmer"})
        for o in lo["hard-rock"]["events"]["ev"]["odds"]:
            if o["player"]["name"] == "Jared Goff":
                o["selection"]["line"] = 274.5  # different line -> must be skipped without a call
        client = FakeClient({"draftkings": 19.0, "caesars": 21.0, "betmgm": 13.0, "hard-rock": 30.0})
        r = sf.price_event(client, "nfl", EVENT_META, lo, books)
        self.assertEqual(len(r["stacks"]), 9)
        self.assertEqual(len([c for c in client.calls if c[0] == "draftkings"]), 8)  # BUF: WR2->RB1 has no Cook receiving line at DK -> no call
        self.assertEqual([c[0] for c in client.calls if c[0] != "draftkings"], ["caesars"])  # compare: lowest stack only; betmgm lacks a leg, hard-rock differs on lines
        low = r["stacks"][0]
        self.assertEqual((low["reference_book"], low["ref_decimal"]), ("draftkings", 19.0))
        self.assertEqual([b["book"] for b in low["books"]], ["draftkings", "caesars", "betmgm", "hard-rock"])
        self.assertEqual(low["compare_books"][0]["book"], "caesars")
        self.assertAlmostEqual(low["compare_books"][0]["gap_pct"], round((21.0 - 19.0) / 19.0 * 100, 1))
        hr = next(b for b in low["books"] if b["book"] == "hard-rock")
        self.assertEqual(hr["compare_skipped"], "different lines - not priced")
        self.assertEqual(hr["legs"][0]["line"], 274.5)
        self.assertEqual(next(b for b in low["books"] if b["book"] == "betmgm")["compare_skipped"], "missing legs")
        self.assertTrue(all([b["book"] for b in s["books"]] == ["draftkings"] for s in r["stacks"][1:]))
        cook = next(s for s in r["stacks"] if s["name"] == "BUF: WR2" + chr(0x2192) + "RB1")
        self.assertIsNone(cook["reference_book"])
        self.assertIn("DraftKings is missing: James Cook III rec yds", cook["dk_problem"])
        self.assertEqual(r["stacks"][-1]["name"], cook["name"])
        self.assertEqual(r["lowest_odds"]["reference_book"], "draftkings")
        self.assertEqual(r["lowest_odds"]["link"], "https://draftkings.test/slip")

    def test_price_not_found_is_shown_as_a_dk_problem_not_a_fallback(self):
        client = FakeClient({"caesars": 20.0})  # draftkings -> 400 Price not found
        r = sf.price_event(client, "nfl", EVENT_META, _league_odds(["draftkings", "caesars"]), ["draftkings", "caesars"])
        self.assertTrue(all(s["reference_book"] is None for s in r["stacks"]))
        self.assertIn("DraftKings: Price not found", [s["dk_problem"] for s in r["stacks"]])
        self.assertNotIn("lowest_odds", r)
        self.assertEqual([c[0] for c in client.calls].count("caesars"), 0)  # no compare pass, no fallback

    def test_budget_trims_variants_in_priority_order_and_notes_it(self):
        books = ["draftkings", "caesars"]
        client = FakeClient({"draftkings": 19.0, "caesars": 18.5}, remaining=3)  # 8 primary calls needed
        r = sf.price_event(client, "nfl", EVENT_META, _league_odds(books), books, compare=False)
        names = [s["name"] for s in r["stacks"]]
        self.assertIn("Base", names)
        self.assertTrue(r["budget_trimmed"])
        self.assertTrue(any("budget guard" in n for n in r["notes"]))
        self.assertNotIn("DET: WR2" + chr(0x2192) + "RB1", names)
        self.assertLessEqual(len(client.calls), 3)

    def test_trim_keeps_base_when_even_base_does_not_fit(self):
        stacks = [{"name": "Base", "variant": None}, {"name": "v", "variant": ("WR2", "TE1")}]
        kept, note = sf.trim_to_budget(stacks, {"Base": ["a", "b", "c"], "v": ["a", "b"]}, remaining=2)
        self.assertEqual([s["name"] for s in kept], ["Base"])
        self.assertIn("only the Base stack", note)

    def test_no_trim_when_budget_is_enough(self):
        stacks = [{"name": "Base", "variant": None}, {"name": "v", "variant": ("WR2", "TE1")}]
        kept, note = sf.trim_to_budget(stacks, {"Base": ["a"], "v": ["a"]}, remaining=5)
        self.assertEqual(len(kept), 2)
        self.assertIsNone(note)


def _row(book, dec, lines, prices=None, correlation=None, priced=True, error=None):
    prices = prices or ["-110"] * 6
    row = {"book": book, "legs": [{"line": lines[i], "price": prices[i], "note": None} for i in range(6)], "missing": [], "notes": [], "priced": priced}
    if priced:
        row.update({"decimal": dec, "american": sf.american_from_decimal(dec), "link": f"https://{book}.test/x", "correlation": correlation})
    elif error:
        row["error"] = error
    else:
        row.update({"missing": ["x rec yds (no prop)"], "legs": [None] * 6})
    return row


DK = [270.5, 82.5, 59.5, 251.5, 45.5, 13.5]
LEGS6 = [{"player": f"p{i}", "team": "T", "slot": "S", "market": REC, "side": "Over", "dk_line": DK[i]} for i in range(6)]


class TestRankingCorrelationAndLineParity(unittest.TestCase):
    def test_correlation_ratio_is_naive_product_over_sgp_price(self):
        prices = ["-110", "+100", "-120", "-105", "+110", "-111"]
        row = _row("draftkings", 19.0, DK, prices=prices, correlation=41.0)
        naive = 1
        for p in prices:
            naive *= sf.decimal_from_american(p)
        self.assertAlmostEqual(sf.naive_decimal(row), round(naive, 3))
        s = sf.rank_stacks([{"name": "Base", "variant": None, "legs": LEGS6, "books": [row]}])[0]
        self.assertAlmostEqual(s["correlation_ratio"], round(round(naive, 3) / 19.0, 3))
        self.assertEqual(s["implied"], round(1 / 19.0, 4))
        self.assertEqual(s["dk_correlation"], 41.0)
        self.assertEqual(s["ref_link"], "https://draftkings.test/x")

    def test_sorted_by_lowest_dk_price_then_higher_correlation_unpriced_last(self):
        a = {"name": "A", "variant": None, "legs": LEGS6, "books": [_row("draftkings", 19.0, DK)]}
        b = {"name": "B", "variant": ("WR2", "TE1"), "legs": LEGS6, "books": [_row("draftkings", 14.0, DK, prices=["-110"] * 6)]}
        c = {"name": "C", "variant": ("WR2", "WR3"), "legs": LEGS6, "books": [_row("draftkings", 14.0, DK, prices=["-150"] * 6)]}
        d = {"name": "D", "variant": ("WR1", "TE1"), "legs": LEGS6, "books": [_row("draftkings", 0, DK, priced=False, error="Price not found"), _row("caesars", 12.0, DK)]}
        ranked = sf.rank_stacks([a, b, c, d])
        self.assertEqual([s["name"] for s in ranked], ["B", "C", "A", "D"])
        self.assertIsNone(ranked[-1]["reference_book"])
        self.assertEqual(ranked[-1]["dk_problem"], "DraftKings: Price not found")
        self.assertEqual(ranked[-1]["compare_books"], [])

    def test_compare_uses_only_other_books_on_the_same_lines(self):
        stack = {"name": "Base", "variant": None, "legs": LEGS6, "books": [
            _row("draftkings", 19.0, DK, correlation=41.0), _row("caesars", 18.5, DK), _row("betrivers", 20.0, DK),
            _row("hard-rock", 32.0, [274.5, 79.5, 24.5, 249.5, 49.5, 24.5]), _row("betmgm", 0, DK, priced=False)]}
        s = sf.rank_stacks([stack])[0]
        self.assertEqual([c["book"] for c in s["compare_books"]], ["betrivers", "caesars"])
        self.assertAlmostEqual(s["compare_books"][0]["gap_pct"], round((20.0 - 19.0) / 19.0 * 100, 1))
        self.assertEqual((s["best_book"], s["best_decimal"]), ("betrivers", 20.0))
        self.assertEqual(s["different_line_books"][0]["book"], "hard-rock")
        self.assertEqual(s["books_priced"], 4)
        self.assertEqual(s["reference_lines"], DK)


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
