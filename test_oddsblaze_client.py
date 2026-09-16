"""Unit tests for oddsblaze_client.py - budget guard (per-minute window,
spacing, persisted daily cap), 429 backoff, key redaction in saved raw
files, and offline replay. Fake HTTP session; no network.

Run with: python -m unittest test_oddsblaze_client -v
"""

import json
import os
import shutil
import tempfile
import unittest

import oddsblaze_client as oc


class FakeResp:
    def __init__(self, status, body=None, text=None, headers=None, url="https://odds.oddsblaze.com/?sportsbook=dk&key=SECRETKEY123456"):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.content = self.text.encode()
        self.headers = headers or {}
        self.url = url
        self.request = type("R", (), {"method": "GET"})()

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.headers = {}
        self.calls = []

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        o = self.outcomes.pop(0)
        if isinstance(o, BaseException):
            raise o
        return o


class TestBudgetGuard(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.path = os.path.join(self.d, "budget.json")

    def _guard(self, **kw):
        self.clock = [0.0]
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            self.clock[0] += s
        g = oc.BudgetGuard(per_minute={"odds": 3, "sgp": 2}, spacing={"odds": 0.0, "sgp": 6.0}, daily_cap={"odds": 5, "sgp": 4}, path=self.path, sleep_fn=sleep, clock=lambda: self.clock[0], **kw)
        return g, sleeps

    def test_per_minute_window_blocks_until_a_slot_frees(self):
        g, sleeps = self._guard()
        for _ in range(3):
            g.wait("odds")
        self.assertEqual(sleeps, [])
        g.wait("odds")  # 4th within the window -> must sleep until the first ages out
        self.assertTrue(sleeps and sleeps[0] >= 59)
        self.assertEqual(g.used_today("odds"), 4)

    def test_spacing_enforced_for_sgp(self):
        g, sleeps = self._guard()
        g.wait("sgp")
        g.wait("sgp")
        self.assertAlmostEqual(sleeps[0], 6.0, places=3)

    def test_daily_cap_persists_and_raises(self):
        g, _ = self._guard()
        for _ in range(4):
            g.wait("sgp")
            self.clock[0] += 61
        self.assertEqual(g.remaining("sgp"), 0)
        with self.assertRaises(oc.BudgetExhausted):
            g.wait("sgp")
        g2, _ = self._guard()  # a new process on the same day sees the same count
        self.assertEqual(g2.used_today("sgp"), 4)
        self.assertEqual(g2.remaining("odds"), 5)
        self.assertEqual(g2.summary()["sgp"]["daily_cap"], 4)


class TestClient(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.raw = os.path.join(self.d, "raw")
        self.sleeps = []
        self.guard = oc.BudgetGuard(path=os.path.join(self.d, "budget.json"), sleep_fn=lambda s: None, clock=lambda: 0.0, spacing={"odds": 0, "sgp": 0})

    def _client(self, outcomes, offline=False):
        session = FakeSession(outcomes)
        c = oc.OddsBlazeClient(key="SECRETKEY123456", guard=self.guard, session=session, raw_dir=self.raw, offline=offline, sleep_fn=self.sleeps.append, rng=lambda: 1.0)
        return c, session

    def test_saves_raw_with_key_redacted_and_sends_key_only_in_params(self):
        c, s = self._client([FakeResp(200, {"events": [], "updated": "x"})])
        st, body = c.odds("draftkings", "nfl", market="player-passing-yards")
        self.assertEqual(st, 200)
        self.assertEqual(s.calls[0]["params"]["key"], "SECRETKEY123456")
        files = os.listdir(os.path.join(self.raw, "odds_draftkings_nfl"))
        self.assertEqual(len(files), 1)
        saved = open(os.path.join(self.raw, "odds_draftkings_nfl", files[0]), encoding="utf-8").read()
        self.assertNotIn("SECRETKEY123456", saved)
        self.assertIn("key=<redacted>", saved)
        meta = json.loads(saved)["meta"]
        self.assertEqual(meta["params"], {"sportsbook": "draftkings", "league": "nfl", "market": "player-passing-yards"})
        self.assertEqual(c.calls["odds"], 1)

    def test_sgp_sends_plain_token_array(self):
        c, s = self._client([FakeResp(200, {"price": "4.10"})])
        st, body = c.sgp("draftkings", ["t1", "t2"])
        self.assertEqual(s.calls[0]["json"], ["t1", "t2"])
        self.assertEqual(s.calls[0]["params"]["price"], "decimal")
        self.assertEqual(body["price"], "4.10")

    def test_429_backs_off_with_retry_after_then_succeeds(self):
        c, s = self._client([FakeResp(429, None, text="slow", headers={"Retry-After": "3"}), FakeResp(200, {"price": "4.10"})])
        st, body = c.sgp("draftkings", ["t1"])
        self.assertEqual(st, 200)
        self.assertEqual(self.sleeps, [3.0])
        self.assertIsNotNone(self.guard.state.get("last_429_at"))

    def test_403_raises_access_blocked_without_retry(self):
        c, s = self._client([FakeResp(403, {"message": "forbidden"})])
        with self.assertRaises(oc.AccessBlocked):
            c.odds("draftkings", "nfl")
        self.assertEqual(len(s.calls), 1)

    def test_missing_key_raises_unless_offline(self):
        with unittest.mock.patch.dict(os.environ, {"ODDSBLAZE_API_KEY": ""}):
            with self.assertRaises(oc.NoKey):
                oc.OddsBlazeClient(key=None, guard=self.guard, raw_dir=self.raw)
            oc.OddsBlazeClient(key=None, guard=self.guard, raw_dir=self.raw, offline=True)

    def test_offline_replays_newest_saved_matching_response(self):
        c, s = self._client([FakeResp(200, {"events": [{"id": "old"}]}), FakeResp(200, {"events": [{"id": "new"}]})])
        c.odds("draftkings", "nfl", market="m")
        c.odds("draftkings", "nfl", market="m")
        off, _ = self._client([], offline=True)
        st, body = off.odds("draftkings", "nfl", market="m")
        self.assertEqual(st, 200)
        self.assertEqual(body["events"][0]["id"], "new")
        self.assertIn("_replayed_from", body)
        self.assertEqual(off.calls["replayed"], 1)
        with self.assertRaises(oc.InvalidResponse):
            off.odds("caesars", "nfl", market="m")  # nothing saved for this signature


if __name__ == "__main__":
    import unittest.mock
    unittest.main()
