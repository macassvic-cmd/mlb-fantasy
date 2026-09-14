"""
Sport-specific soft-tier classifiers for watchlist_dns.py's at-risk
watchlist (added 2026-09-11) - separate from rss_news_classifier.py
(MLB, feeds the HARD detector's shadow source) even though it reuses
that module's title/pubDate parsing, which is sport-agnostic.

NFL and soccer each get their OWN vocabulary rather than reusing MLB's:
confirmed live 2026-09-11 that RotoWire's NFL feed uses the same literal
words MLB's classifier already expects ("questionable", "doubtful",
"cleared to play" - see NFL_SOFT_TIER_PATTERNS), but soccer coverage
uses a genuinely different idiom ("late call", "assessed", "option for",
a fitness test before a match) that a plain reuse of MLB's patterns
would simply never match - confirmed by inspecting real live soccer
headlines during the Phase 0 feasibility check ("Cody Gakpo: Late call
against Fulham", "James Maddison: Option for Everton clash").

TRANSFERMARKT'S "BORDERLINE RETURN TIMING" TIER: Transfermarkt's own
injury/suspension table (scrapers/transfermarkt.py) does NOT carry a
doubtful/questionable probability tier at all - confirmed live by
inspecting a real club's table: every row is a confirmed diagnosis with
a since-date, never a likelihood grade. The only usable proxy is a
player who's still LISTED (not yet cleared off the table) but whose own
expected_return_date falls on/near the upcoming fixture - genuinely
uncertain (may not be fully sharp, rotation risk, the date could slip)
but a DIFFERENT and WEAKER kind of signal than an actual "doubtful"
tag, so it's tagged "borderline-return-timing", never "doubtful" or
"questionable" - see borderline_return_timing_hits().

TIER_RANK gives every tier (across all three sports/sources) a numeric
severity for the digest's sort order - lower rank sorts first (more
likely to actually miss the game). Deliberately NOT one shared label
vocabulary across sports (soccer's "late-call"/"fifty-fifty" are real,
distinct idioms, not reworded synonyms for "questionable"/"doubtful") -
only the sort order is unified, not the tier names themselves.
"""

import re
from datetime import datetime, timezone

from rss_news_classifier import parse_title, parse_rss_pubdate, RSS_URL as MLB_RSS_URL
from scrapers.betr import normalize_name

NFL_RSS_URL = "https://www.rotowire.com/rss/news.php?sport=NFL"
SOCCER_RSS_URL = "https://www.rotowire.com/rss/news.php?sport=Soccer"

# Same fallback net as rss_news_classifier.INJURY_ADJACENT_PATTERNS -
# imported from there rather than redefined, so the two never drift.
from rss_news_classifier import INJURY_ADJACENT_PATTERNS

# ---------------------------------------------------------------------------
# NFL - RotoWire feed, confirmed live 2026-09-11 using near-identical
# vocabulary to MLB's (see module docstring).
# ---------------------------------------------------------------------------
NFL_SOFT_TIER_PATTERNS = {
    "doubtful": [r"\bdoubtful\b"],
    "game-time-decision": [r"\bgame-time decision\b", r"\bgametime decision\b"],
    "questionable": [r"\bquestionable\b", r"\blisted as questionable\b"],
    "probable": [r"\bprobable\b", r"\bexpected to play\b", r"\bwill play\b"],
    "no-designation": [r"\bno designation\b"],
}
NFL_HARD_OUT_PATTERNS = [
    r"\bruled out\b", r"\bwill not play\b", r"\bwon'?t play\b", r"\bplaced on injured reserve\b",
    r"\bplaced on ir\b", r"\bwaived\b", r"\bcarted off\b", r"\bout for the season\b",
]
NFL_REVERSAL_PATTERNS = [r"\bcleared to play\b", r"\bactivated\b", r"\bno longer\b.*\b(questionable|doubtful|out)\b"]

# ---------------------------------------------------------------------------
# Soccer - RotoWire feed, OWN vocabulary (confirmed live 2026-09-11 against
# real headlines - see module docstring). "fifty-fifty"/"late-call" sit at
# GTD-equivalent severity (see TIER_RANK) without claiming to BE "GTD" -
# soccer coverage doesn't use that term.
# ---------------------------------------------------------------------------
SOCCER_SOFT_TIER_PATTERNS = {
    "fifty-fifty": [r"\b50-50\b", r"\bfifty-fifty\b", r"\btouch and go\b"],
    "late-call": [r"\blate call\b", r"\blate decision\b", r"\bassessed on (the day|matchday)\b"],
    "fitness-test": [r"\bfitness test\b", r"\bundergo(es)? a fitness test\b"],
    "monitored": [r"\bmonitored\b", r"\bbeing monitored\b", r"\bunder observation\b"],
    "assessed": [r"\bassessed\b", r"\bassessment\b"],
    "option": [r"\boption for\b", r"\bin contention for\b", r"\ba doubt for\b", r"\bdoubtful for\b"],
}
SOCCER_HARD_OUT_PATTERNS = [
    r"\bruled out\b", r"\bwill miss\b", r"\bsidelined\b", r"\bout (for|of)\b",
    # Added 2026-09-14 auditing why real injury headlines were slipping through
    # unclassified ("Ian Maatsen: Out six to eight weeks", "Dean Henderson:
    # Won't return before break" both matched NOTHING under the original four
    # patterns) - RotoWire's soccer headlines often state a duration/timeframe
    # rather than the word "for"/"of" right after "out".
    r"\bout (\w+|\d+)( to (\w+|\d+))? (days?|weeks?|months?)\b",
    r"\bwon'?t return\b", r"\bwill not return\b", r"\bexpected to miss\b", r"\bout until\b",
    r"\bundergo(es|ing|ne)? surgery\b", r"\brequires? surgery\b",
]
SOCCER_REVERSAL_PATTERNS = [r"\bfully fit\b", r"\bavailable for\b", r"\bcleared to play\b", r"\breturns? to training\b"]

_COMPILED = {}


def _compile_all():
    if _COMPILED:
        return _COMPILED
    _COMPILED["nfl_soft"] = {t: [re.compile(p, re.I) for p in pats] for t, pats in NFL_SOFT_TIER_PATTERNS.items()}
    _COMPILED["nfl_hard"] = [re.compile(p, re.I) for p in NFL_HARD_OUT_PATTERNS]
    _COMPILED["nfl_reversal"] = [re.compile(p, re.I) for p in NFL_REVERSAL_PATTERNS]
    _COMPILED["soccer_soft"] = {t: [re.compile(p, re.I) for p in pats] for t, pats in SOCCER_SOFT_TIER_PATTERNS.items()}
    _COMPILED["soccer_hard"] = [re.compile(p, re.I) for p in SOCCER_HARD_OUT_PATTERNS]
    _COMPILED["soccer_reversal"] = [re.compile(p, re.I) for p in SOCCER_REVERSAL_PATTERNS]
    _COMPILED["injury_adjacent"] = [re.compile(p, re.I) for p in INJURY_ADJACENT_PATTERNS]
    return _COMPILED


def _classify_generic(headline, soft_patterns, hard_patterns, reversal_patterns):
    """Shared engine for NFL/soccer: reversal wins over everything (same
    precedence classify_transaction()/rss_news_classifier.classify()
    already use), then hard-out (NOT what this watchlist wants, but worth
    naming distinctly so a caller can skip it rather than mis-tag it as
    soft), then the sport's own soft tiers (first match wins, in the
    order the dict was built), then the shared injury-adjacent fallback
    -> "news-mention", else None (not a watchlist hit at all)."""
    text = headline or ""
    c = _compile_all()
    if any(p.search(text) for p in reversal_patterns):
        return "reversal", None
    if any(p.search(text) for p in hard_patterns):
        return "hard_out", None
    for tier, patterns in soft_patterns.items():
        if any(p.search(text) for p in patterns):
            return "soft", tier
    if any(p.search(text) for p in c["injury_adjacent"]):
        return "news_mention", "news-mention"
    return None, None


def classify_nfl_headline(headline):
    c = _compile_all()
    return _classify_generic(headline, c["nfl_soft"], c["nfl_hard"], c["nfl_reversal"])


def classify_soccer_headline(headline):
    c = _compile_all()
    return _classify_generic(headline, c["soccer_soft"], c["soccer_hard"], c["soccer_reversal"])


def _fetch_rss_items(url):
    import urllib.request
    import xml.etree.ElementTree as ET

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=20).read()
    root = ET.fromstring(raw)
    items = []
    for item in root.findall(".//item"):
        title = item.findtext("title")
        guid = item.findtext("guid")
        pub_date = item.findtext("pubDate")
        if not guid or not title:
            continue
        items.append({"title": title, "guid": guid, "pub_date": pub_date})
    return items


def fetch_nfl_watchlist_items(url=NFL_RSS_URL):
    """[{name, normalized_name, headline, classification, tier, guid,
    pub_date, pub_date_utc}] for every RotoWire NFL RSS item currently in
    the feed whose classification is "soft" or "news_mention" - reversal
    and hard_out items are fetched (so the classifier sees the full
    feed - a reversal headline still needs to be recognized as NOT a
    watchlist hit) but filtered out of the returned list, since this
    function feeds a WATCHLIST, not the hard detector."""
    results = []
    for raw_item in _fetch_rss_items(url):
        name, headline = parse_title(raw_item["title"])
        if name is None:
            continue
        classification, tier = classify_nfl_headline(headline)
        if classification not in ("soft", "news_mention"):
            continue
        pub_date_utc = parse_rss_pubdate(raw_item["pub_date"])
        results.append({
            "name": name, "normalized_name": normalize_name(name),
            "headline": headline, "classification": classification, "tier": tier,
            "guid": raw_item["guid"], "pub_date": raw_item["pub_date"],
            "pub_date_utc": pub_date_utc.isoformat() if pub_date_utc else None,
        })
    return results


def fetch_soccer_watchlist_items(url=SOCCER_RSS_URL):
    """Same shape/filtering as fetch_nfl_watchlist_items(), classified
    against SOCCER_SOFT_TIER_PATTERNS instead."""
    results = []
    for raw_item in _fetch_rss_items(url):
        name, headline = parse_title(raw_item["title"])
        if name is None:
            continue
        classification, tier = classify_soccer_headline(headline)
        if classification not in ("soft", "news_mention"):
            continue
        pub_date_utc = parse_rss_pubdate(raw_item["pub_date"])
        results.append({
            "name": name, "normalized_name": normalize_name(name),
            "headline": headline, "classification": classification, "tier": tier,
            "guid": raw_item["guid"], "pub_date": raw_item["pub_date"],
            "pub_date_utc": pub_date_utc.isoformat() if pub_date_utc else None,
        })
    return results


# ---------------------------------------------------------------------------
# Transfermarkt "borderline return timing" - see module docstring for why
# this is NOT labeled as a probability tier.
# ---------------------------------------------------------------------------
BORDERLINE_RETURN_WINDOW_DAYS = 3


def borderline_return_timing_hits(injury_records, fixture_date):
    """injury_records: scrapers.transfermarkt.fetch_team_injuries()/
    get_team_injuries_cached()'s own return shape (already has
    since_date/expected_return_date as parsed date objects). Returns
    every record still on the list whose expected_return_date exists and
    falls within BORDERLINE_RETURN_WINDOW_DAYS of fixture_date (before OR
    after - a return date just AFTER the fixture is exactly as uncertain
    as one just before it, since injury-recovery timelines routinely
    slip a few days in either direction). Records with NO expected_return
    _date (indefinite absence) are excluded here entirely - those are the
    hard detector's confirmed-out territory, not a borderline case."""
    if fixture_date is None:
        return []
    hits = []
    for r in injury_records:
        ret = r.get("expected_return_date")
        if ret is None:
            continue
        delta_days = abs((ret - fixture_date).days)
        if delta_days <= BORDERLINE_RETURN_WINDOW_DAYS:
            hits.append({
                "name": r["name"], "normalized_name": r["normalized_name"],
                "tier": "borderline-return-timing",
                "raw_text": f"{r['reason']} - expected back {r['expected_return']} (since {r['since']})",
                "expected_return_date": ret.isoformat(),
                "days_from_fixture": (ret - fixture_date).days,
            })
    return hits


# Sort order for the digest - lower rank surfaces first (more likely to
# actually miss the game). NOT a claim that e.g. NFL "doubtful" and
# soccer "fifty-fifty" mean exactly the same thing - just a practical
# ordering so the digest reads most-to-least concerning across sources
# that don't share a common taxonomy.
TIER_RANK = {
    "doubtful": 1,
    "fifty-fifty": 1,
    "game-time-decision": 2,
    "late-call": 2,
    "gtd": 2,
    "questionable": 3,
    "fitness-test": 3,
    "limited-practice": 3,
    "dnp": 3,
    "borderline-return-timing": 4,
    "day-to-day": 4,
    "monitored": 4,
    "assessed": 4,
    "option": 5,
    "probable": 5,
    "no-designation": 6,
    "nearing-return": 6,
    "news-mention": 7,
}


def tier_rank(tier):
    return TIER_RANK.get(tier, 8)  # unknown tier sorts last, not crashes
