"""
Classifier for RotoWire's public MLB RSS feed, wired into stale_lines.py
(see check_news() there) as a THIRD early-signal source alongside
check_transactions/check_roster_status - added 2026-09-11 after
diagnosing 5 of the 6 tracked MLB early-signal misses (Gasper, Greene,
Smith, Wood, Griffin). SHADOW MODE ONLY: check_news() writes into the
same state["early_signals"] dict, scored by the same
_resolve_early_signals pass, with NO Discord wiring - same restriction
the other two sources are still under, "until lead time and accuracy are
known" (see stale_lines.py's module docstring). Let it accumulate real
outcomes before trusting it for anything live.

WHY THIS EXISTS: diagnosing 5 of MLB's 6 tracked early-signal misses
(Gasper, Greene, Smith, Wood, Griffin - see events.jsonl/state.json)
found that in every case the real "activated ... from the injured list"
/ "recalled" transaction DID exist and DID contain a REVERSAL_KEYWORDS
match - classify_transaction()'s keyword logic isn't the problem. The
failure is TIMING: none of those reversal transactions were visible via
/api/v1/transactions before the real lineup already confirmed the
player, even though the transaction's own "date" field (day-only, no
time-of-day) matched the same calendar day. Mickey Gasper's case is
worse: his reversal (id 940249, "recalled", dated 2026-08-31) predates
his OUT signal's own creation (2026-09-01T17:29) by nearly a full day
and STILL wasn't caught by _resolve_reversals before the game - meaning
even a same-poll reversal check against an already-fetched, already-
in-window transaction can lose this race. MLB's own roster-paperwork
API is not a reliable real-time signal; the player's own beat-reporter
news usually is (this project's best real catch, Jazz Chisholm, came
from news, not a transaction).

FEED CHECKED LIVE (2026-09-11): https://www.rotowire.com/rss/news.php?
sport=MLB - HTTP 200, no auth, plain RSS/XML, real browser UA. Only
returned 5 items covering roughly the last 20 hours at check time - this
is a short ROLLING window, not an archive. Confirmed via the Wayback
Machine's CDX API that no historical snapshot of this exact feed exists
anywhere close to 2026 (only two dead 2021 captures) - so there is no
way to retroactively backtest this classifier against the Aug 31-Sep 4
misses above; that comparison can only be done PROSPECTIVELY, which is
exactly what check_news() running in shadow mode now does - each item's
own <guid> (confirmed present, e.g. "mlb1024974" - stable per RotoWire
item) is the dedup key check_news() uses, the same role a transaction's
own id plays for check_transactions().

CLASSIFICATION - same shape as espn_pro_leagues.LEAGUE_CONFIG's
confirmed_out_statuses/not_confirmed_statuses split (which already works
well for NFL): CONFIRMED_OUT_PATTERNS only match language that asserts
the player is not playing TODAY/this game/for an extended period - this
is what feeds stale_lines.check_news() (the hard DNS detector's shadow
source). SOFT_TIER_PATTERNS (day-to-day, questionable, doubtful, game-
time decision, probable, nearing-return) are deliberately excluded from
the confirmed-out bucket, even though "day-to-day" was named as one of
the original keywords to catch - a bare day-to-day tag is exactly the
same ambiguous, often-plays-anyway signal "questionable" is for the pro
leagues, and folding it into the confirmed tier would just reintroduce
the false-positive problem this file exists to avoid. These soft tiers
are exactly what watchlist_dns.py's soft at-risk watchlist (added
2026-09-11) wants instead - see classify_soft_tier() and
classify_feed_item()'s "tier" field. REVERSAL_PATTERNS (activated,
reinstated, recalled, cleared to play, starting tonight) mirror
stale_lines.REVERSAL_KEYWORDS so a news item can also close an already-
open signal from ANY source, not just another news item - the same
cross-source reversal check that _resolve_reversals does for
transactions.
"""

import re
from datetime import datetime, timedelta, timezone

from scrapers.betr import normalize_name

RSS_URL = "https://www.rotowire.com/rss/news.php?sport=MLB"

# RotoWire's pubDate is RFC-822-shaped but with a 12-hour clock + AM/PM
# (standard RFC-822 uses 24-hour time, so email.utils.parsedate_to_
# datetime doesn't parse it) and a US zone abbreviation instead of a
# numeric offset - confirmed live 2026-09-11, every item used "PDT".
# Covering the other continental US zones defensively in case a future
# item uses a different one; an unrecognized abbreviation just leaves
# pub_date_utc as None (raw pub_date string is always kept too).
_TZ_OFFSETS = {
    "PST": -8, "PDT": -7, "MST": -7, "MDT": -6,
    "CST": -6, "CDT": -5, "EST": -5, "EDT": -4,
    "UTC": 0, "GMT": 0,
}
_PUBDATE_RE = re.compile(
    r"^\w+,\s+(\d{1,2})\s+(\w+)\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s+(AM|PM)\s+(\w+)$"
)


def parse_rss_pubdate(s):
    """RotoWire pubDate string -> aware UTC datetime, or None if it
    doesn't match the expected shape or uses an unrecognized zone
    abbreviation."""
    m = _PUBDATE_RE.match((s or "").strip())
    if not m:
        return None
    day, mon_name, year, hour, minute, second, ampm, tz = m.groups()
    try:
        month = datetime.strptime(mon_name, "%b").month
    except ValueError:
        return None
    offset_hours = _TZ_OFFSETS.get(tz.upper())
    if offset_hours is None:
        return None
    hour = int(hour) % 12
    if ampm.upper() == "PM":
        hour += 12
    dt = datetime(int(year), month, int(day), hour, int(minute), int(second),
                   tzinfo=timezone(timedelta(hours=offset_hours)))
    return dt.astimezone(timezone.utc)

# Ordered by specificity - checked in this order so a headline matching
# BOTH a reversal and an out-pattern (e.g. "activated, but still day-to-
# day") resolves as reversal-wins, same precedence classify_transaction()
# already uses (REVERSAL_KEYWORDS checked before the OUT typeCodes).
REVERSAL_PATTERNS = [
    r"\bactivated\b", r"\breinstated\b", r"\brecalled\b",
    r"\bcleared to play\b", r"\bwill start\b", r"\bstarting (tonight|today|sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b",
    r"\bback in the lineup\b", r"\bno longer\b.*\b(questionable|doubtful|out)\b",
]

# High-confidence ONLY - language that asserts the player is not
# available for the CURRENT/next game, not a vague future-tense injury
# mention. Deliberately narrower than a plain "out" keyword: RotoWire
# headlines routinely use "out" in ways that aren't a DNS signal at all
# ("Cordero out of minor league options", "out for the year" said about
# someone already known to be out for months - noise, not new
# information for a player who currently has a posted line).
CONFIRMED_OUT_PATTERNS = [
    r"\bscratched from\b", r"\bscratched (tonight|today)?'?s?\s*lineup\b",
    r"\bwill not start\b", r"\bwon'?t start\b", r"\bnot in (tonight|today)'?s? lineup\b",
    r"\bruled out\b", r"\bwill not play\b", r"\bwon'?t play\b",
    r"\bplaced on the (\d+-day )?injured list\b", r"\bplaced on the il\b",
    r"\bout for the (season|year)\b", r"\bundergoes? surgery\b",
    r"\bbenched (tonight|today|for)\b", r"\bsitting (tonight|today|out)\b",
    r"\bheld out\b", r"\bwill miss (tonight|today|the rest)\b",
]

# NOT a confirmed-out signal on their own - same reasoning as
# espn_pro_leagues.LEAGUE_CONFIG's not_confirmed_statuses. Grouped by the
# SPECIFIC soft tier (added 2026-09-11 for the at-risk watchlist - see
# watchlist_dns.py) rather than one flat list, so a caller can tag which
# tier actually matched instead of a generic "not_confirmed" bucket.
# Checked in this order (most-committal first) when more than one phrase
# could apply to the same headline.
SOFT_TIER_PATTERNS = {
    "doubtful": [r"\bdoubtful\b"],
    "game-time-decision": [r"\bgame-time decision\b", r"\bgametime decision\b"],
    "questionable": [r"\bquestionable\b"],
    "probable": [r"\bprobable\b", r"\bexpected to play\b"],
    "day-to-day": [r"\bday-to-day\b", r"\bday to day\b"],
    "nearing-return": [r"\bnearing (a )?return\b", r"\bcould return\b", r"\bprogressing\b", r"\btrending toward\b"],
}
# Flattened for the coarse "not_confirmed" bucket check in classify() -
# kept as a derived view (not hand-duplicated) so the two can never drift.
NOT_CONFIRMED_PATTERNS = [p for pats in SOFT_TIER_PATTERNS.values() for p in pats]

# Fallback net for the watchlist's "news-mention" catch-all (added
# 2026-09-11, see watchlist_dns.py's module docstring point 4): a
# headline that matches NONE of the specific vocabularies above but still
# reads as injury/fitness-adjacent shouldn't be silently dropped -
# tagged "news_mention" (lowest confidence) instead of "unclassified"
# (nothing injury-related detected at all, e.g. "Homers in back-to-back
# games"). Deliberately broad/noisy on individual body-part words - it's
# only ever a fallback for the LOWEST-confidence tier, never a gate on
# anything actionable, so false positives here (a headline that mentions
# "shoulder" in a non-injury context) cost a spurious watchlist row, not
# a false DNS bet.
INJURY_ADJACENT_PATTERNS = [
    r"\binjur", r"\bknock\b", r"\bissue\b", r"\bconcern\b", r"\bfitness\b",
    r"\bstrain\b", r"\bsore(ness)?\b", r"\btight(ness)?\b", r"\bill(ness)?\b",
    r"\bsick\b", r"\bsurger", r"\bscan\b", r"\bsetback\b", r"\bprecaution",
    r"\brest(ed|ing)?\b", r"\bworkload\b", r"\bhamstring\b", r"\bankle\b",
    r"\bknee\b", r"\bshoulder\b", r"\bback (spasm|issue|tightness)\b",
    r"\bhealth\b", r"\bmedical\b", r"\bleaves? (the )?game\b",
]

_COMPILED = {
    "reversal": [re.compile(p, re.I) for p in REVERSAL_PATTERNS],
    "confirmed_out": [re.compile(p, re.I) for p in CONFIRMED_OUT_PATTERNS],
    "not_confirmed": [re.compile(p, re.I) for p in NOT_CONFIRMED_PATTERNS],
    "injury_adjacent": [re.compile(p, re.I) for p in INJURY_ADJACENT_PATTERNS],
}
_COMPILED_SOFT_TIERS = {tier: [re.compile(p, re.I) for p in pats] for tier, pats in SOFT_TIER_PATTERNS.items()}


def classify_soft_tier(headline):
    """Which SOFT_TIER_PATTERNS key matched (e.g. "doubtful",
    "questionable"), or None if the headline doesn't match any of them.
    Only meaningful to call when classify() already returned
    "not_confirmed" - this just tells you which specific tier within
    that bucket."""
    text = headline or ""
    for tier, patterns in _COMPILED_SOFT_TIERS.items():
        if any(p.search(text) for p in patterns):
            return tier
    return None

# RotoWire's own title format, confirmed live 2026-09-11 across every
# item in the feed: "PlayerName: headline text". Colon is the separator
# RotoWire itself uses, not something inferred - a title with no colon
# doesn't match this shape at all and is left unclassified rather than
# guessed at.
_TITLE_RE = re.compile(r"^([^:]+):\s*(.+)$")


def parse_title(title):
    """("Riley Greene", "Activated from the 10-day injured list") or
    (None, title) if the title doesn't match RotoWire's "Name: headline"
    shape at all."""
    m = _TITLE_RE.match(title or "")
    if not m:
        return None, title
    return m.group(1).strip(), m.group(2).strip()


def classify(headline):
    """One of "reversal", "confirmed_out", "not_confirmed", "news_mention"
    (added 2026-09-11 - matches none of the three known vocabularies but
    still reads as injury-adjacent, see INJURY_ADJACENT_PATTERNS), or
    "unclassified" (doesn't match anything at all, e.g. "Homers in back-
    to-back games" - not injury-related in any way). check_news() in
    stale_lines.py only acts on "confirmed_out"/"reversal", so the
    "news_mention" addition doesn't change the hard detector's behavior
    at all - it's purely additive for watchlist_dns.py to consume."""
    text = headline or ""
    for kind in ("reversal", "confirmed_out", "not_confirmed"):
        if any(p.search(text) for p in _COMPILED[kind]):
            return kind
    if any(p.search(text) for p in _COMPILED["injury_adjacent"]):
        return "news_mention"
    return "unclassified"


def classify_feed_item(title):
    """(player_name, normalized_name, headline, classification, tier) for
    one RSS <item>'s <title>, or None if the title doesn't parse. "tier"
    is the specific SOFT_TIER_PATTERNS match when classification is
    "not_confirmed" (e.g. "questionable", "doubtful"), the literal string
    "news-mention" when classification is "news_mention", or None
    otherwise (reversal/confirmed_out/unclassified don't have a finer
    tier - the classification itself is the answer)."""
    name, headline = parse_title(title)
    if name is None:
        return None
    classification = classify(headline)
    if classification == "not_confirmed":
        tier = classify_soft_tier(headline)
    elif classification == "news_mention":
        tier = "news-mention"
    else:
        tier = None
    return {
        "name": name,
        "normalized_name": normalize_name(name),
        "headline": headline,
        "classification": classification,
        "tier": tier,
    }


def fetch_and_classify(url=RSS_URL):
    """Live smoke test only - fetches the feed right now and classifies
    whatever's currently in it. NOT a backtest (see module docstring for
    why a real backtest against historical data isn't possible) - this
    just proves the parser/classifier work against real, live RotoWire
    titles, and gives a starting point for prospective shadow-logging."""
    import urllib.request
    import xml.etree.ElementTree as ET

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=20).read()
    root = ET.fromstring(raw)

    results = []
    for item in root.findall(".//item"):
        title = item.findtext("title")
        guid = item.findtext("guid")
        pub_date = item.findtext("pubDate")
        parsed = classify_feed_item(title)
        if parsed is None or not guid:
            continue
        pub_date_utc = parse_rss_pubdate(pub_date)
        parsed["guid"] = guid
        parsed["pub_date"] = pub_date
        parsed["pub_date_utc"] = pub_date_utc.isoformat() if pub_date_utc else None
        parsed["fetched_at_utc"] = datetime.now(timezone.utc).isoformat()
        results.append(parsed)
    return results


if __name__ == "__main__":
    for r in fetch_and_classify():
        print(f"[{r['classification']:14s}] {r['pub_date']} | {r['name']}: {r['headline']}")
