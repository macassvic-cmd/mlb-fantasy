"""Unit tests for discord_health.py - focused on the 2026-09-15 security
fix: a raised requests exception's own str(e) commonly embeds the full
request URL, which for a Discord webhook POST IS a bearer credential
(anyone holding it can post to that channel). Nothing derived from an
exception may reach data/soccer_discord_health.json (or, transitively,
docs/soccer-dns.html) without being redacted first.

Run with: python -m unittest test_discord_health -v
"""

import json
import os
import tempfile
import unittest
from unittest.mock import patch

import discord_health as dh

FAKE_WEBHOOK_URL = "https://discord.com/api/webhooks/123456789012345678/AbCdEf-fake-token-ghijklmnop"


class TestRedact(unittest.TestCase):
    def test_strips_a_discord_webhook_url(self):
        text = f"ConnectionError: Failed to establish a new connection to {FAKE_WEBHOOK_URL}"
        result = dh.redact(text)
        self.assertNotIn(FAKE_WEBHOOK_URL, result)
        self.assertIn("[REDACTED_WEBHOOK_URL]", result)

    def test_strips_discordapp_com_variant_too(self):
        url = "https://discordapp.com/api/webhooks/999/some-token-value-here"
        result = dh.redact(f"error hitting {url}")
        self.assertNotIn(url, result)

    def test_strips_the_literal_value_of_a_secret_shaped_env_var(self):
        with patch.dict(os.environ, {"DISCORD_SOCCER_DNS_WEBHOOK_URL": "supersecretvalue12345"}):
            result = dh.redact("failed calling supersecretvalue12345 endpoint")
        self.assertNotIn("supersecretvalue12345", result)
        self.assertIn("[REDACTED_DISCORD_SOCCER_DNS_WEBHOOK_URL]", result)

    def test_does_not_redact_a_non_secret_shaped_env_var(self):
        with patch.dict(os.environ, {"SOME_HARMLESS_VAR": "harmlessvalue123"}):
            result = dh.redact("mentions harmlessvalue123 here")
        self.assertIn("harmlessvalue123", result)

    def test_short_env_values_are_not_redacted_to_avoid_false_positives(self):
        with patch.dict(os.environ, {"MY_TOKEN": "abc"}):
            result = dh.redact("the abc here is unrelated")
        self.assertIn("abc", result, "a 3-char secret value must not blanket-redact common substrings")

    def test_none_and_empty_are_safe_no_ops(self):
        self.assertIsNone(dh.redact(None))
        self.assertEqual(dh.redact(""), "")

    def test_non_string_input_returned_unchanged_not_a_crash(self):
        self.assertEqual(dh.redact(404), 404)

    def test_clean_text_with_nothing_secret_is_unchanged(self):
        self.assertEqual(dh.redact("HTTPError (HTTP 400)"), "HTTPError (HTTP 400)")


class TestRecordAttemptRedactsBeforePersisting(unittest.TestCase):
    """The exact scenario found live: a raised requests ConnectionError
    whose str(e) contains the real webhook URL must never end up in
    data/soccer_discord_health.json."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch_path = patch.object(dh, "HEALTH_PATH", os.path.join(self._tmp.name, "health.json"))
        self._patch_path.start()

    def tearDown(self):
        self._patch_path.stop()
        self._tmp.cleanup()

    def test_connection_error_containing_webhook_url_produces_a_clean_health_record(self):
        try:
            import requests
            raise requests.exceptions.ConnectionError(
                f"HTTPSConnectionPool(host='discord.com', port=443): Max retries exceeded with "
                f"url: /api/webhooks/123456789012345678/AbCdEf-fake-token-ghijklmnop"
            )
        except Exception as e:
            # Mirrors the sanitized-at-the-source pattern soccer_alerts.py
            # and soccer_daily_digest.py now use - type + status only,
            # never str(e) - plus record_attempt's own redact() backstop.
            error_message = f"{type(e).__name__} (HTTP None)"
            state = dh.record_attempt("realtime_alert", False, http_status=None, error=error_message)

        self.assertNotIn("api/webhooks", json.dumps(state))
        self.assertNotIn("AbCdEf-fake-token-ghijklmnop", json.dumps(state))

        with open(dh.HEALTH_PATH, encoding="utf-8") as f:
            on_disk = f.read()
        self.assertNotIn("api/webhooks", on_disk)
        self.assertNotIn("AbCdEf-fake-token-ghijklmnop", on_disk)

    def test_record_attempt_redacts_even_if_a_caller_forgets_to_sanitize(self):
        """Backstop: even an unsanitized error string passed directly
        must never survive into the persisted record."""
        state = dh.record_attempt("daily_digest", False, http_status=None,
                                   error=f"ConnectionError: could not reach {FAKE_WEBHOOK_URL}")
        self.assertNotIn(FAKE_WEBHOOK_URL, json.dumps(state))
        self.assertIn("[REDACTED_WEBHOOK_URL]", state["last_error"])

    def test_successful_attempt_never_touches_last_error(self):
        state = dh.record_attempt("realtime_alert", True, http_status=204)
        self.assertIsNone(state.get("last_error"))


if __name__ == "__main__":
    unittest.main()
