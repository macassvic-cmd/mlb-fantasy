"""
Once-daily check: is Betr's GraphQL API public again?

Added 2026-09-08 after Betr locked api.fantasy.betr.app/graphql behind
real HTTP Basic Auth (WWW-Authenticate: Basic realm="Realm"), which
disabled both the MLB stale-line detector (stale_lines_local.py /
stale_lines.yml) and soccer_dns.py / soccer_dns.yml - indefinitely
polling a deliberately-gated endpoint has no value, but leaving those
detectors off forever on faith isn't right either. This is the cheap
alternative: one request a day, alert once if access is restored, so a
reversal wouldn't just go unnoticed indefinitely.

Deliberately reuses the exact same query stale_lines.py/soccer_dns.py
already use (not a lighter probe) - the point is to test the actual
access path those detectors need, not just "does the server respond to
something."
"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

GRAPHQL_URL = "https://api.fantasy.betr.app/graphql"
HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
UPCOMING_EVENTS_QUERY = """query UpcomingEventsInfo($league: League!) {
  getUpcomingEventsV2(league: $league) { ... on EventV2 { id } }
}"""

STATE_PATH = os.path.join("data", "betr_access_check.json")


def is_betr_public():
    """True only on a clean 200 with a real GraphQL payload (no
    "errors" key) - not just "not a 401", since some other failure
    mode (5xx, a different auth scheme, a malformed response) shouldn't
    read as "access restored" either."""
    body = json.dumps({"query": UPCOMING_EVENTS_QUERY, "variables": {"league": "MLB"}}).encode()
    req = urllib.request.Request(GRAPHQL_URL, data=body, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read())
            return "errors" not in payload and "data" in payload
    except Exception:
        return False


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"already_alerted_public": False}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def post_alert(webhook_url):
    body = {"embeds": [{
        "title": "\U0001F7E2 Betr's API appears public again",
        "description": (
            "Today's daily check got a clean response from "
            "api.fantasy.betr.app/graphql instead of 401 - the endpoint "
            "may no longer require auth. The MLB stale-line detector and "
            "soccer_dns.py were disabled on 2026-09-08 when this got "
            "locked down (data/stale_lines/DISABLED, and both "
            "stale_lines.yml and soccer_dns.yml disabled on GitHub) - "
            "re-enable them if this holds."
        ),
        "color": 0x2ECC71,
    }]}
    req = urllib.request.Request(
        webhook_url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
    )
    urllib.request.urlopen(req, timeout=15)


def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    public = is_betr_public()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")

    if public:
        print("Betr's endpoint returned a clean response - appears public.")
        if not state.get("already_alerted_public"):
            if webhook_url:
                try:
                    post_alert(webhook_url)
                    print("ALERTED: Betr's endpoint appears public again.")
                except Exception as e:
                    print(f"Alert POST failed: {e}")
            else:
                print("DISCORD_WEBHOOK_URL not set - no-op.")
            state["already_alerted_public"] = True
            state["confirmed_public_at_utc"] = now.isoformat()
        else:
            print("Already alerted for this - not re-alerting.")
    else:
        print("Betr's endpoint is still gated (401 or otherwise unavailable) - no action.")
        # Reset the dedup flag once it goes back to gated, so a LATER
        # genuine re-opening alerts again rather than being silently
        # suppressed forever by one earlier blip that didn't hold.
        state["already_alerted_public"] = False

    state["last_checked_utc"] = now.isoformat()
    save_state(state)


if __name__ == "__main__":
    main()
