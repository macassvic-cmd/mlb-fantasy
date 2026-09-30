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


def format_kickoff_pacific(event_date_iso):
    """'Tue 9/16, 12:00 PM PDT' from a raw event_date ISO string (e.g.
    '2026-09-16T19:00:00.000Z') - added 2026-09-16 (Hinshelwood item 4):
    alerts (Discord + dashboard) were showing that raw UTC string as-is,
    which nobody reading an alert should have to mentally convert. Falls
    back to the original string unchanged on anything unparseable -
    never raises, never hides a real (if malformed) value."""
    if not event_date_iso:
        return event_date_iso
    try:
        dt = datetime.fromisoformat(event_date_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(PACIFIC_TZ)
        # %-m/%-d/%-I (no leading zero) are Linux-only strftime codes -
        # this runs on both GitHub Actions (Linux) and the user's own
        # Windows PC (see CLAUDE.md), where they raise ValueError - build
        # the no-leading-zero pieces by hand instead.
        hour12 = local.hour % 12 or 12
        return (f"{local.strftime('%a')} {local.month}/{local.day}, "
                f"{hour12}:{local.strftime('%M %p %Z')}")
    except Exception:
        return event_date_iso
