"""
Shared "what day is it" helper for the whole Soccer DNS stack (2026-09-14
production-scheduling fix) - same lesson as scrapers.mlb_api.mlb_
today_str: a bare `datetime.now(timezone.utc).strftime("%Y-%m-%d")`
silently rolls "today" over up to 7-8 hours before Pacific evening does,
which is confusing at best (a digest/snapshot/alert-log stamped
"tomorrow" while every human involved still calls it "today") given this
product's whole daily rhythm (the digest schedule, --if-due, the user's
own day) is Pacific-anchored - confirmed live 2026-09-14 running the
digest at 22:53 PT / 05:53 UTC the next day, which silently filed that
day's snapshot/digest/alerts under "2026-09-15" before this fix.

Soccer kickoffs themselves are mostly European evening (US Pacific
morning/midday) and don't need this - this is specifically about which
CALENDAR DAY a snapshot/digest/cache file belongs to from the operator's
own point of view.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def soccer_today_str(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(PACIFIC_TZ).strftime("%Y-%m-%d")
