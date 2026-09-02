"""
Fetches club injury/suspension lists from Transfermarkt (transfermarkt.com) -
a public, unauthenticated source: the page's cookie-consent banner
(Sourcepoint) is client-side only, the injury table itself is server-
rendered in the raw HTML with a plain browser User-Agent, no consent
cookie or JS execution required. Confirmed 2026-09-02 - not bot-protected
in any meaningful sense, unlike SofaScore (blocked at the network/edge
level even for the bare homepage, from this environment).

Two-step lookup per club: resolve_team() maps a club's full legal name
(the shape Betr's GraphQL API returns, e.g. "Ballspielverein Borussia 09
Dortmund") to a Transfermarkt slug+id via the site's own quick-search,
then fetch_team_injuries() pulls that club's
"Suspensions and injuries" page and parses the table into structured
records: player, reason, since-date, expected-return-date.

Both steps are cached to disk (data/transfermarkt/) - resolve_team()
forever (a club's TM id doesn't change), fetch_team_injuries() per
calendar day (injury lists update daily, not per-poll - this is a once-
a-day scan, not a live poller like stale_lines.py).
"""

import json
import logging
import os
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join("data", "transfermarkt")
TEAM_CACHE_PATH = os.path.join(CACHE_DIR, "team_resolution.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# Betr returns a club's full LEGAL name (e.g. "Fußball-Club Bayern,
# München", "Ballspielverein Borussia 09 Dortmund"), which
# Transfermarkt's own search index matches poorly against - it's built
# around the common/colloquial name every site actually calls the club.
# Hand-mapped once for the ~20 clubs (mostly Bundesliga/Ligue 1/MLS/EPL)
# that failed a direct full-name search during the 2026-09-02 feasibility
# check. Not meant to be exhaustive - resolve_team() tries the raw name
# first and only falls back to this map, so a club not listed here just
# means the direct search already worked.
TEAM_SEARCH_ALIASES = {
    "Ballspielverein Borussia 09 Dortmund": "Borussia Dortmund",
    "Fußball-Club Bayern, München": "Bayern Munich",
    "Turn- und Sportgemeinschaft 1899 Hoffenheim": "TSG Hoffenheim",
    "Verein für Bewegungsspiele Stuttgart 1893": "VfB Stuttgart",
    "Sport-Club Paderborn 07": "SC Paderborn 07",
    "Borussia Verein-für-Leibesübungen 1900 Mönchengladbach": "Borussia Monchengladbach",
    "1. Fußball- und Sport-Verein Mainz 05": "Mainz 05",
    "1. Fußball-Club Köln 01/07": "1. FC Koln",
    "Olympique Gynmaste Club de Nice-Côte-d'Azur": "OGC Nice",
    "Le Havre Athletic Club Football Association": "Le Havre AC",
    "Association Sportive de Monaco Football Club": "AS Monaco",
    "Brighton & Hove Albion Football Club": "Brighton and Hove Albion",
    "Leeds United Football Club": "Leeds United",
    "D.C. United Soccer Club": "D.C. United",
    "Austin Football Club": "Austin FC",
    "Minnesota United Football Club": "Minnesota United FC",
    "Seattle Sounders Football Club": "Seattle Sounders FC",
    "Vancouver Whitecaps Football Club": "Vancouver Whitecaps FC",
    "Toronto Football Club": "Toronto FC",
    "Nashville Soccer Club": "Nashville SC",
    "Hamburger Sport-Verein": "Hamburger SV",
    "Chicago Fire Football Club": "Chicago Fire FC",
    "New York Red Bulls": "Red Bull New York",
    "Torino Football Club": "Torino FC",
    "Espérance Sportive Troyes Aube Champagne": "ESTAC Troyes",
}

# Reserve/youth squads share the parent club's name on Transfermarkt's
# search (e.g. searching "Chicago Fire Football Club" surfaces
# "chicago-fire-fc-2" - the reserve side - above or alongside the senior
# team). A club's INJURY LIST only matters for the first team, so any
# search result whose slug or display text carries one of these markers
# is skipped in favor of the next candidate.
RESERVE_SLUG_MARKERS = re.compile(r"-(ii|u1[4-9]|u2[0-3]|b|2|ii-2|reserves?)(?:$|-)", re.IGNORECASE)
RESERVE_TEXT_MARKERS = re.compile(r"\b(U1[4-9]|U2[0-3]|II|B-?Team|Reserves?|Youth|Academy)\b")


def normalize_name(name):
    name = unicodedata.normalize("NFKD", name or "")
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.lower()
    name = re.sub(r"[^a-z0-9 ]", "", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()


def _fetch_html(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _load_team_cache():
    if os.path.exists(TEAM_CACHE_PATH):
        with open(TEAM_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_team_cache(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(TEAM_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def _search_team_once(query):
    """One quick-search call - returns (slug, team_id) for the first
    non-reserve/youth club result, or (None, None). Transfermarkt's
    search already ranks by relevance/market value, so the first
    survivor of the reserve-squad filter is reliably the senior team."""
    url = "https://www.transfermarkt.com/schnellsuche/ergebnis/schnellsuche?query=" + urllib.parse.quote(query)
    html = _fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.match(r"^(/[^/]+)/startseite/verein/(\d+)$", href)
        if not m:
            continue
        slug, team_id = m.group(1), m.group(2)
        text = a.get_text(strip=True)
        if RESERVE_SLUG_MARKERS.search(slug) or RESERVE_TEXT_MARKERS.search(text):
            continue
        return slug, team_id
    return None, None


def resolve_team(full_name):
    """(slug, team_id) for a club's Transfermarkt profile, cached forever
    (a club's TM id doesn't change season to season). Tries the raw
    Betr full_name first, then TEAM_SEARCH_ALIASES if that fails."""
    cache = _load_team_cache()
    if full_name in cache:
        entry = cache[full_name]
        return entry["slug"], entry["team_id"]

    slug, team_id = _search_team_once(full_name)
    if not slug and full_name in TEAM_SEARCH_ALIASES:
        time.sleep(0.5)
        slug, team_id = _search_team_once(TEAM_SEARCH_ALIASES[full_name])

    cache[full_name] = {"slug": slug, "team_id": team_id, "resolved_at": datetime.now(timezone.utc).isoformat()}
    _save_team_cache(cache)
    return slug, team_id


_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def parse_date_ddmmyyyy(s):
    """Transfermarkt dates are DD/MM/YYYY. Returns a date or None for a
    blank cell (an indefinite injury with no announced return date)."""
    m = _DATE_RE.match((s or "").strip())
    if not m:
        return None
    day, month, year = m.groups()
    try:
        return datetime(int(year), int(month), int(day)).date()
    except ValueError:
        return None


def fetch_team_injuries(slug, team_id):
    """[{name, normalized_name, reason, since, since_date, expected_return,
    expected_return_date}] from a club's "Suspensions and injuries" page -
    covers BOTH sections Transfermarkt lists there (long-term injuries and
    active suspensions), since either one means "not available," which is
    all a DNS bet cares about. *_date fields are parsed date objects (or
    None for blank/unparseable), for the fixture-date comparison in
    soccer_dns.py; the raw *_since/*_expected_return strings are kept
    for the Discord alert/sanity-check display."""
    url = f"https://www.transfermarkt.com{slug}/sperrenundverletzungen/verein/{team_id}"
    html = _fetch_html(url)
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.items")
    if not table:
        return []

    records = []
    section = None
    for tr in table.select("tbody > tr"):
        header_td = tr.select_one("td.extrarow")
        if header_td:
            section = header_td.get_text(strip=True)
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 6:
            continue
        name_link = tr.select_one("td.hauptlink a")
        if not name_link:
            continue
        name = name_link.get_text(strip=True)
        reason = tds[2].get_text(strip=True)
        since = tds[3].get_text(strip=True)
        expected_return = tds[4].get_text(strip=True)
        records.append({
            "section": section,
            "name": name,
            "normalized_name": normalize_name(name),
            "reason": reason,
            "since": since,
            "since_date": parse_date_ddmmyyyy(since),
            "expected_return": expected_return,
            "expected_return_date": parse_date_ddmmyyyy(expected_return),
        })
    return records


def get_team_injuries_cached(full_name, cache_date_str):
    """fetch_team_injuries() cached to data/transfermarkt/injuries/
    {cache_date_str}/{team_id}.json - one fetch per club per calendar day,
    since this module backs a once-daily scan (soccer_dns.py), not a
    live poller. Returns [] (not None) if the club can't be resolved on
    Transfermarkt at all, so callers don't need a separate not-found
    branch - an unresolved club just contributes zero injury records."""
    slug, team_id = resolve_team(full_name)
    if not slug:
        logger.warning(f"Transfermarkt: no club match for {full_name!r}")
        return []

    day_dir = os.path.join(CACHE_DIR, "injuries", cache_date_str)
    cache_path = os.path.join(day_dir, f"{team_id}.json")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        for r in cached:
            r["since_date"] = parse_date_ddmmyyyy(r["since"])
            r["expected_return_date"] = parse_date_ddmmyyyy(r["expected_return"])
        return cached

    records = fetch_team_injuries(slug, team_id)
    os.makedirs(day_dir, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump([{k: v for k, v in r.items() if not k.endswith("_date")} for r in records],
                   f, indent=2, ensure_ascii=False)
    return records
