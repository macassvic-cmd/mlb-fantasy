"""Unit tests for play_card.py (rev 3: DraftKings primary, no DNS on the
card) - lowest-odds stack per game, DK problems shown, optional same-lines
compare, freshness tiers, Discord embeds/chunking and the HTML page.
top_dns_plays() is still tested because sgp_bot's /dns uses it. Temp dirs
only; no network.

Run with: python -m unittest test_play_card -v
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import play_card as pc
import stack_forge

NOW = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)  # Sun 9/20 9:00 AM PT
ARROW = chr(0x2192)


def _snapshot(path, now=NOW):
    soon = (now + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    past = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    data = {
        "_latest_batch_ts": (now - timedelta(minutes=20)).isoformat(),
        "pid:1": {"name": "A Player", "team": "OSA", "latest": {"player_name": "A Player", "team": "OSA", "matchup": "OSA @ ATM", "league_code": "LLG", "event_date": soon,
                                                                "official_status": "not_yet_posted", "transfermarkt_injury": {"reason": "hamstring"}, "rotowire_status_normalized": "out",
                                                                "rotowire_player_page_url": "https://www.rotowire.com/soccer/player/a-player-1"},
                  "score_history": [{"ts": (now - timedelta(minutes=25)).isoformat(), "dns_score": 88, "confidence_score": 70}]},
        "pid:3": {"name": "Kicked Off", "team": "XXX", "latest": {"player_name": "Kicked Off", "team": "XXX", "matchup": "XXX @ YYY", "event_date": past},
                  "score_history": [{"ts": (now - timedelta(minutes=25)).isoformat(), "dns_score": 99, "confidence_score": 99}]},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


LEGS = [{"team": "DET", "slot": "QB1", "player": "Jared Goff", "player_norm": "jaredgoff", "market": "Player Passing Yards", "side": "Over", "dk_line": 270.5, "position": "QB", "position_flag": None},
        {"team": "DET", "slot": "WR1", "player": "ARSB", "player_norm": "arsb", "market": "Player Receiving Yards", "side": "Over", "dk_line": 82.5, "position": "WR", "position_flag": None},
        {"team": "DET", "slot": "WR2", "player": "JW", "player_norm": "jw", "market": "Player Receiving Yards", "side": "Over", "dk_line": 59.5, "position": "WR", "position_flag": None},
        {"team": "BUF", "slot": "QB1", "player": "Josh Allen", "player_norm": "joshallen", "market": "Player Passing Yards", "side": "Over", "dk_line": 251.5, "position": "QB", "position_flag": None},
        {"team": "BUF", "slot": "WR1", "player": "KS", "player_norm": "ks", "market": "Player Receiving Yards", "side": "Over", "dk_line": 45.5, "position": "WR", "position_flag": None},
        {"team": "BUF", "slot": "WR2", "player": "JP", "player_norm": "jp", "market": "Player Receiving Yards", "side": "Over", "dk_line": 13.5, "position": "WR", "position_flag": "position inferred from passing-yards prop"}]
DK_LINES = [270.5, 82.5, 59.5, 251.5, 45.5, 13.5]
HR_LINES = [274.5, 79.5, 24.5, 249.5, 49.5, 24.5]


def _book(b, dec, lines=None, priced=True, sgp_at=None, replayed=False, correlation=None, error=None):
    row = {"book": b, "legs": [{"line": (lines or DK_LINES)[i], "price": "-110", "note": None} for i in range(6)], "missing": [], "notes": [], "priced": priced}
    if priced:
        row.update({"decimal": dec, "american": stack_forge.american_from_decimal(dec), "implied": round(1 / dec, 4), "link": f"https://{b}.test/x", "sgp_at": sgp_at, "correlation": correlation})
        if replayed:
            row["replayed_from"] = "2026-09-19T00:00:00+00:00"
    elif error:
        row["error"] = error
    else:
        row["missing"] = ["JP rec yds (no prop)"]
        row["legs"] = [None] * 6
    return row


def _result(now=NOW, kickoff_offset_h=8, replayed=False, sgp_age_min=10):
    sgp_at = (now - timedelta(minutes=sgp_age_min)).isoformat()
    k = dict(sgp_at=sgp_at, replayed=replayed)
    stacks = [
        {"name": "Base", "variant": None, "legs": LEGS, "books": [_book("draftkings", 19.0, correlation=41.0, **k)]},
        {"name": f"DET: WR2{ARROW}TE1", "variant": ["WR2", "TE1"], "legs": LEGS, "books": [_book("draftkings", 14.0, correlation=50.0, **k), _book("betrivers", 15.0, **k), _book("caesars", 13.5, **k),
                                                                                         _book("hard-rock", 30.0, lines=HR_LINES, **k), _book("betmgm", 0, priced=False)]},
        {"name": f"BUF: WR1{ARROW}TE1", "variant": ["WR1", "TE1"], "legs": LEGS, "books": [_book("draftkings", 16.0, correlation=48.0, **k)]},
        {"name": f"DET: WR2{ARROW}WR3", "variant": ["WR2", "WR3"], "legs": LEGS, "books": [_book("draftkings", 0, priced=False, error="Price not found")]},
        {"name": f"BUF: WR2{ARROW}RB1", "variant": ["WR2", "RB1"], "legs": LEGS, "books": [_book("draftkings", 0, priced=False)]},
    ]
    stacks = stack_forge.rank_stacks(stacks)
    return {"league": "nfl", "event": "ev1", "away": "DET", "home": "BUF", "kickoff": (now + timedelta(hours=kickoff_offset_h)).isoformat().replace("+00:00", "Z"),
            "stacks": stacks, "notes": [f"budget guard: dropped variant(s) WR2{ARROW}RB1"] if replayed else [], "budget_trimmed": replayed, "roster": {}}


class TestDnsPlaysForTheBot(unittest.TestCase):
    def test_kickoff_gate_sources_link_and_read_only(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "2026-09-20.json")
        _snapshot(path)
        with open(path, encoding="utf-8") as f:
            before = f.read()
        dns = pc.top_dns_plays(path, now=NOW)
        self.assertEqual([p["player"] for p in dns["plays"]], ["A Player"])
        self.assertIn("Transfermarkt: hamstring", dns["plays"][0]["sources"])
        self.assertEqual(dns["plays"][0]["rotowire_url"], "https://www.rotowire.com/soccer/player/a-player-1")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(pc.top_dns_plays(os.path.join(d, "nope.json"), now=NOW)["plays"], [])


class TestCard(unittest.TestCase):
    def test_lowest_odds_stack_at_dk_with_correlation_and_ranked_rest(self):
        card = pc.build_card(["nfl"], results=[_result()], now=NOW)
        self.assertNotIn("dns", card)
        self.assertEqual(card["primary"], "draftkings")
        self.assertEqual(card["overall_tier"], "fresh")
        g = card["games"][0]
        low = g["lowest"]
        self.assertEqual((low["name"], low["decimal"], low["american"]), (f"DET: WR2{ARROW}TE1", 14.0, 1300))
        self.assertAlmostEqual(low["implied"], round(1 / 14.0, 4))
        self.assertAlmostEqual(low["naive_decimal"], round((1 + 100 / 110) ** 6, 3))
        self.assertAlmostEqual(low["correlation_ratio"], round(low["naive_decimal"] / 14.0, 3))
        self.assertEqual(low["dk_correlation"], 50.0)
        self.assertEqual(low["link"], "https://draftkings.test/x")
        self.assertEqual([l["line"] for l in low["legs"]], DK_LINES)
        self.assertEqual([v["decimal"] for v in g["ranked"]], [14.0, 16.0, 19.0])
        self.assertEqual((g["variants_priced"], g["variants_total"]), (3, 5))

    def test_dk_problems_are_shown_not_hidden(self):
        card = pc.build_card(["nfl"], results=[_result()], now=NOW)
        problems = {p["name"]: p["why"] for p in card["games"][0]["dk_problems"]}
        self.assertEqual(problems[f"DET: WR2{ARROW}WR3"], "DraftKings: Price not found")
        self.assertIn("DraftKings is missing: JP rec yds", problems[f"BUF: WR2{ARROW}RB1"])
        self.assertTrue(any("DraftKings could not price" in w for w in card["warnings"]))

    def test_compare_is_same_lines_only_and_off_by_default_on_the_page(self):
        card = pc.build_card(["nfl"], results=[_result()], now=NOW)
        low = card["games"][0]["lowest"]
        self.assertEqual([c["abbr"] for c in low["compare"]], ["BR", "CZR"])  # highest payout first, DK excluded
        self.assertAlmostEqual(low["compare"][0]["gap_pct"], round((15.0 - 14.0) / 14.0 * 100, 1))
        skipped = {s["abbr"]: s["why"] for s in low["compare_skipped"]}
        self.assertEqual(skipped["HR"], "different lines - not compared")
        self.assertEqual(skipped["MGM"], "missing legs")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        page = pc.render_html(card, os.path.join(d, "sgp.html"))
        with open(page, encoding="utf-8") as f:
            page_text = f.read()
        self.assertIn(".compare { display: none; }", page_text)
        self.assertIn("id=\"compareToggle\"", page_text)
        self.assertNotIn("checked", page_text.split("compareToggle")[1][:40])
        self.assertIn("+7.1% vs DK", page_text)
        self.assertIn("lines 274.5/79.5/24.5/249.5/49.5/24.5", page_text)

    def test_kicked_off_game_is_dropped(self):
        card = pc.build_card(["nfl"], results=[_result(kickoff_offset_h=-1)], now=NOW)
        self.assertEqual(card["games"], [])

    def test_freshness_tiers_from_sgp_pull_time_only(self):
        self.assertEqual(pc.build_card(["nfl"], results=[_result(sgp_age_min=10)], now=NOW)["overall_tier"], "fresh")
        warn = pc.build_card(["nfl"], results=[_result(sgp_age_min=90)], now=NOW)
        self.assertEqual(warn["overall_tier"], "warn")
        self.assertTrue(any("SGP prices are WARN" in w for w in warn["warnings"]))
        self.assertEqual(pc.build_card(["nfl"], results=[_result(sgp_age_min=200)], now=NOW)["overall_tier"], "stale")

    def test_saved_without_key_is_forced_stale(self):
        card = pc.build_card(["nfl"], results=[_result(replayed=True)], now=NOW, forced_stale=True, source="saved (NoKey)")
        self.assertEqual(card["overall_tier"], "stale")
        self.assertTrue(all(v["stale"] for v in card["games"][0]["ranked"]))
        self.assertTrue(any("last saved prices" in w for w in card["warnings"]))
        self.assertTrue(any("budget guard" in w for w in card["warnings"]))

    def test_discord_embeds_and_html_no_dns_no_secrets(self):
        with mock.patch.dict(os.environ, {"ODDSBLAZE_API_KEY": "SHOULDNOTLEAK"}):
            card = pc.build_card(["nfl"], results=[_result()], now=NOW)
            embeds = pc.discord_embeds(card)
            d = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, d, True)
            page = pc.render_html(card, os.path.join(d, "sgp.html"))
        text = json.dumps(embeds)
        self.assertNotIn("DNS", text)
        self.assertIn("Lowest-odds stack: DET: WR2", text)
        self.assertIn("implied **7.1%**", text)
        self.assertIn("DK corr 50.0", text)
        self.assertIn("Next by odds:", text)
        self.assertIn("DraftKings could not price:", text)
        self.assertIn("Price not found", text)
        with open(page, encoding="utf-8") as f:
            page_text = f.read()
        for needle in ("viewport", "freshness-banner fresh", "sgp.html\" class=\"active\"", "Lowest-odds stack:", "All variants at DraftKings, lowest odds first", "DK corr", "Bet at DK +1300", "position inferred", "DraftKings could not price"):
            self.assertIn(needle, page_text, needle)
        self.assertNotIn("Top DNS", page_text)
        self.assertNotIn("SHOULDNOTLEAK", page_text)

    def test_embeds_are_chunked_under_discord_char_budget(self):
        big = [{"title": f"t{i}", "description": "x" * 2500, "color": 1} for i in range(6)]
        batches = pc.chunk_embeds(big)
        self.assertTrue(all(sum(len(e["title"]) + len(e["description"]) for e in b) <= pc.DISCORD_MSG_CHAR_BUDGET for b in batches))
        self.assertEqual(sum(len(b) for b in batches), 6)

    def test_post_discord_records_health(self):
        card = pc.build_card(["nfl"], results=[_result()], now=NOW)
        calls = []
        fake_resp = mock.Mock(status_code=204)
        fake_resp.raise_for_status = lambda: None
        with mock.patch("play_card.requests.post", return_value=fake_resp) as post, mock.patch("discord_health.record_attempt", side_effect=lambda *a, **k: calls.append((a, k))):
            ok = pc.post_discord(card, webhook_url="https://discord.com/api/webhooks/1/secret")
        self.assertTrue(ok)
        self.assertIn("DraftKings", post.call_args[1]["json"]["embeds"][0]["title"])
        self.assertEqual(calls[0][0], ("play_card", True))


class TestRun(unittest.TestCase):
    def test_price_without_key_builds_from_saved_marked_stale(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.dict(os.environ, {"ODDSBLAZE_API_KEY": ""}), mock.patch("stack_forge.load_results", return_value=[_result()]):
            card, page = pc.run(["nfl"], price=True, page_path=os.path.join(d, "sgp.html"), card_path=os.path.join(d, "latest.json"))
        self.assertTrue(card["source"].startswith("saved"))
        self.assertEqual(card["overall_tier"], "stale")
        with open(page, encoding="utf-8") as f:
            self.assertIn("STALE", f.read())

    def test_from_saved_uses_real_pull_time_for_freshness(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch("stack_forge.load_results", return_value=[_result(now=datetime.now(timezone.utc), sgp_age_min=5)]):
            card, _ = pc.run(["nfl"], from_saved=True, page_path=os.path.join(d, "sgp.html"), card_path=os.path.join(d, "latest.json"))
        self.assertEqual(card["source"], "saved")
        self.assertEqual(card["overall_tier"], "fresh")


if __name__ == "__main__":
    unittest.main()
