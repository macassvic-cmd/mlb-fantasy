"""Unit tests for play_card.py - DNS top plays (read-only, kickoff-gated),
card assembly, freshness tiers, Discord embeds and the HTML page. Every
file write goes to a temp dir; no network (Discord POST is mocked).

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

NOW = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)  # Sun 9/20 9:00 AM PT


def _snapshot(path, now=NOW):
    soon = (now + timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    past = (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    data = {
        "_latest_batch_ts": (now - timedelta(minutes=20)).isoformat(),
        "pid:1": {"name": "A Player", "team": "OSA", "latest": {"player_name": "A Player", "team": "OSA", "matchup": "OSA @ ATM", "league_code": "LLG", "event_date": soon,
                                                                "official_status": "not_yet_posted", "transfermarkt_injury": {"reason": "hamstring"}, "rotowire_status_normalized": "out",
                                                                "rotowire_player_page_url": "https://www.rotowire.com/soccer/player/a-player-1", "predicted_bench_sources": ["recent_start_pattern"]},
                  "score_history": [{"ts": (now - timedelta(minutes=25)).isoformat(), "dns_score": 88, "confidence_score": 70}]},
        "pid:2": {"name": "B Player", "team": "LEE", "latest": {"player_name": "B Player", "team": "LEE", "matchup": "LEE @ NEW", "league_code": "EPL", "event_date": soon, "official_status": "not_yet_posted"},
                  "score_history": [{"ts": (now - timedelta(minutes=25)).isoformat(), "dns_score": 72, "confidence_score": 40}]},
        "pid:3": {"name": "Kicked Off", "team": "XXX", "latest": {"player_name": "Kicked Off", "team": "XXX", "matchup": "XXX @ YYY", "event_date": past},
                  "score_history": [{"ts": (now - timedelta(minutes=25)).isoformat(), "dns_score": 99, "confidence_score": 99}]},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def _result(now=NOW, kickoff_offset_h=8, replayed=False, sgp_age_min=10):
    sgp_at = (now - timedelta(minutes=sgp_age_min)).isoformat()
    legs = [{"team": "DET", "slot": "QB1", "player": "Jared Goff", "player_norm": "jaredgoff", "market": "Player Passing Yards", "side": "Over", "dk_line": 270.5, "position": "QB", "position_flag": None}] + \
           [{"team": "DET", "slot": s, "player": p, "player_norm": p.lower(), "market": "Player Receiving Yards", "side": "Over", "dk_line": 50.5, "position": "WR", "position_flag": None} for s, p in (("WR1", "ARSB"), ("WR2", "JW"))] + \
           [{"team": "BUF", "slot": "QB1", "player": "Josh Allen", "player_norm": "joshallen", "market": "Player Passing Yards", "side": "Over", "dk_line": 251.5, "position": "QB", "position_flag": None}] + \
           [{"team": "BUF", "slot": s, "player": p, "player_norm": p.lower(), "market": "Player Receiving Yards", "side": "Over", "dk_line": 40.5, "position": "WR", "position_flag": "position inferred from passing-yards prop" if s == "WR2" else None} for s, p in (("WR1", "KS"), ("WR2", "JP"))]

    def book(b, dec, priced=True):
        row = {"book": b, "legs": [{"line": 270.5 if i in (0, 3) else 50.5, "price": "-110", "note": None} for i in range(6)], "missing": [] if priced else ["JP rec yds (no prop)"], "notes": [], "priced": priced}
        if priced:
            row.update({"decimal": dec, "american": round((dec - 1) * 100), "implied": round(1 / dec, 4), "link": f"https://{b}.test/x?key=SHOULDNOTLEAK", "sgp_at": sgp_at})
            if replayed:
                row["replayed_from"] = "2026-09-19T00:00:00+00:00"
        return row
    stacks = [
        {"name": "Base", "variant": None, "legs": legs, "books": [book("draftkings", 19.0), book("caesars", 18.5), book("betmgm", 0, priced=False)]},
        {"name": "DET: WR2→TE1", "variant": ["WR2", "TE1"], "legs": legs, "books": [book("draftkings", 20.0), book("hard-rock", 24.0)]},
        {"name": "BUF: WR1→TE1", "variant": ["WR1", "TE1"], "legs": legs, "books": [book("draftkings", 17.0)]},
        {"name": "DET: WR2→WR3", "variant": ["WR2", "WR3"], "legs": legs, "books": [book("draftkings", 16.0)]},
    ]
    import stack_forge
    stacks = stack_forge.rank_stacks(stacks)
    return {"league": "nfl", "event": "ev1", "away": "DET", "home": "BUF", "kickoff": (now + timedelta(hours=kickoff_offset_h)).isoformat().replace("+00:00", "Z"),
            "stacks": stacks, "notes": ["budget guard: dropped variant(s) WR2→RB1"] if replayed else [], "budget_trimmed": replayed, "roster": {},
            "best_overall": {"stack": "DET: WR2→TE1", "book": "hard-rock", "decimal": 24.0, "american": 2300, "link": "https://hard-rock.test/x"}}


class TestDnsPlays(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.path = os.path.join(self.d, "2026-09-20.json")
        self.data = _snapshot(self.path)

    def test_kickoff_gate_sorting_sources_and_link(self):
        dns = pc.top_dns_plays(self.path, now=NOW)
        names = [p["player"] for p in dns["plays"]]
        self.assertEqual(names, ["A Player", "B Player"])  # kicked-off fixture excluded despite DNS 99
        a = dns["plays"][0]
        self.assertEqual((a["dns"], a["confidence"], a["team"], a["league"]), (88, 70, "OSA", "LLG"))
        self.assertIn("Transfermarkt: hamstring", a["sources"])
        self.assertIn("RotoWire: out", a["sources"])
        self.assertIn("predicted bench: recent_start_pattern", a["sources"])
        self.assertEqual(a["rotowire_url"], "https://www.rotowire.com/soccer/player/a-player-1")
        self.assertTrue(a["kickoff_pt"].endswith("PT"))
        self.assertEqual(dns["plays"][1]["sources"], [])
        self.assertEqual(dns["last_refresh"], self.data["_latest_batch_ts"])

    def test_missing_snapshot_is_empty_not_an_error(self):
        dns = pc.top_dns_plays(os.path.join(self.d, "nope.json"), now=NOW)
        self.assertEqual(dns, {"plays": [], "last_refresh": None, "snapshot": None})

    def test_never_writes_to_the_snapshot(self):
        before = open(self.path, encoding="utf-8").read()
        pc.top_dns_plays(self.path, now=NOW)
        self.assertEqual(open(self.path, encoding="utf-8").read(), before)


class TestCard(unittest.TestCase):
    def _dns(self, age_min=20):
        return {"plays": [{"player": "A Player", "team": "OSA", "matchup": "OSA @ ATM", "league": "LLG", "kickoff": "2026-09-20T19:00:00Z", "kickoff_pt": "Sun 9/20 12:00 PM PT", "dns": 88, "confidence": 70, "sources": ["RotoWire: out"], "rotowire_url": None}],
                "last_refresh": (NOW - timedelta(minutes=age_min)).isoformat(), "snapshot": "x"}

    def test_fresh_card_top_stacks_runner_up_and_spread(self):
        card = pc.build_card(["nfl"], results=[_result()], dns=self._dns(), now=NOW)
        self.assertEqual(card["overall_tier"], "fresh")
        self.assertEqual(card["warnings"], [])
        self.assertEqual(len(card["games"]), 1)
        g = card["games"][0]
        self.assertEqual([s["name"] for s in g["stacks"]], ["DET: WR2→TE1", "Base", "BUF: WR1→TE1"])  # top 3 by best price
        top = g["stacks"][0]
        self.assertEqual((top["best_book_abbr"], top["best_american"], top["runner_up_abbr"]), ("HR", 2300, "DK"))
        self.assertAlmostEqual(top["spread_pct"], 20.0)
        self.assertEqual(top["legs"][0]["lines"]["DK"], 270.5)
        self.assertEqual(card["top_stacks"][0]["game"], "DET@BUF")
        self.assertFalse(top["stale"])

    def test_kicked_off_game_is_dropped(self):
        card = pc.build_card(["nfl"], results=[_result(kickoff_offset_h=-1)], dns=self._dns(), now=NOW)
        self.assertEqual(card["games"], [])

    def test_stale_tiers_and_warnings(self):
        card = pc.build_card(["nfl"], results=[_result(sgp_age_min=200)], dns=self._dns(age_min=90), now=NOW)
        self.assertEqual(card["dns"]["tier"], "warn")
        self.assertEqual(card["sgp"]["tier"], "stale")
        self.assertEqual(card["overall_tier"], "stale")
        self.assertTrue(any("DNS data is WARN" in w for w in card["warnings"]))
        self.assertTrue(any("SGP prices are STALE" in w for w in card["warnings"]))

    def test_saved_prices_are_forced_stale_and_budget_note_surfaces(self):
        card = pc.build_card(["nfl"], results=[_result(replayed=True)], dns=self._dns(), now=NOW, forced_stale=True, source="saved")
        self.assertEqual(card["sgp"]["tier"], "stale")
        self.assertTrue(all(s["stale"] for s in card["games"][0]["stacks"]))
        self.assertTrue(any("last saved prices" in w for w in card["warnings"]))
        self.assertTrue(any("budget guard" in w for w in card["warnings"]))

    def test_discord_embeds_and_html_contain_the_card_and_no_secrets(self):
        with mock.patch.dict(os.environ, {"ODDSBLAZE_API_KEY": "SHOULDNOTLEAK"}):
            card = pc.build_card(["nfl"], results=[_result()], dns=self._dns(), now=NOW)
            embeds = pc.discord_embeds(card)
            d = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, d, True)
            page = pc.render_html(card, os.path.join(d, "sgp.html"))
        text = json.dumps(embeds)
        self.assertIn("The Play Card", embeds[0]["title"])
        self.assertIn("A Player", text)
        self.assertIn("HR +2300 (24.00)", text)
        self.assertIn("runner-up DK 20.00 (spread +20.0%)", text)
        html_text = open(page, encoding="utf-8").read()
        for needle in ("viewport", "freshness-banner fresh", "sgp.html\" class=\"active\"", "soccer-dns.html", "index.html", "Jared Goff", "DK 270.5", "+2300", "spread <b>+20.0%</b>", "position inferred", "Open at HR"):
            self.assertIn(needle, html_text, needle)
        self.assertNotIn("SHOULDNOTLEAK", html_text)
        self.assertNotIn("SHOULDNOTLEAK", text)

    def test_post_discord_records_health_and_never_logs_url(self):
        card = pc.build_card(["nfl"], results=[_result()], dns=self._dns(), now=NOW)
        calls = []
        fake_resp = mock.Mock(status_code=204)
        fake_resp.raise_for_status = lambda: None
        with mock.patch("play_card.requests.post", return_value=fake_resp) as post, mock.patch("discord_health.record_attempt", side_effect=lambda *a, **k: calls.append((a, k))):
            ok = pc.post_discord(card, webhook_url="https://discord.com/api/webhooks/1/secret")
        self.assertTrue(ok)
        self.assertEqual(post.call_args[1]["json"]["embeds"][0]["title"], "The Play Card")
        self.assertEqual(calls[0][0], ("play_card", True))

    def test_embeds_are_chunked_under_discord_char_budget(self):
        big = [{"title": f"t{i}", "description": "x" * 2500, "color": 1} for i in range(6)]
        batches = pc.chunk_embeds(big)
        self.assertTrue(all(sum(len(e["title"]) + len(e["description"]) for e in b) <= pc.DISCORD_MSG_CHAR_BUDGET for b in batches))
        self.assertEqual(sum(len(b) for b in batches), 6)
        self.assertEqual(len(batches), 3)

    def test_save_card_writes_atomically(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        card = pc.build_card(["nfl"], results=[_result()], dns=self._dns(), now=NOW)
        path = pc.save_card(card, os.path.join(d, "latest.json"))
        self.assertTrue(os.path.exists(path))
        self.assertFalse(os.path.exists(path + ".tmp"))
        self.assertEqual(json.load(open(path, encoding="utf-8"))["overall_tier"], "fresh")


class TestRunDegradesWithoutKey(unittest.TestCase):
    def test_price_without_key_builds_from_saved_marked_stale(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.dict(os.environ, {"ODDSBLAZE_API_KEY": ""}), \
             mock.patch("stack_forge.load_results", return_value=[_result()]), \
             mock.patch("play_card.top_dns_plays", return_value={"plays": [], "last_refresh": NOW.isoformat(), "snapshot": None}):
            card, page = pc.run(["nfl"], price=True, page_path=os.path.join(d, "sgp.html"), card_path=os.path.join(d, "latest.json"))
        self.assertTrue(card["source"].startswith("saved"))
        self.assertEqual(card["sgp"]["tier"], "stale")
        self.assertIn("STALE", open(page, encoding="utf-8").read())


if __name__ == "__main__":
    unittest.main()
