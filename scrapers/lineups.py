"""
RotoWire lineup scraper — fallback lineup confirmation source.
Used to cross-check that players from the MLB API are confirmed starters.
"""

import logging
import re
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

URL = "https://www.rotowire.com/baseball/daily-lineups.php"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def get_rotowire_lineups():
    """
    Returns {player_name: {team, batting_order, position, confirmed: True}}
    Returns empty dict on any failure — the pipeline continues without it.
    """
    try:
        resp = requests.get(URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"RotoWire fetch failed: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "lxml")
    lineups = {}

    # Each game card has two lineup lists (away / home)
    for card in soup.find_all("div", class_=re.compile(r"lineup__card")):
        team_divs = card.find_all("div", class_=re.compile(r"lineup__team"))
        team_names = [d.get_text(strip=True) for d in team_divs[:2]]

        for side_idx, ul in enumerate(card.find_all("ul", class_=re.compile(r"lineup__list"))[:2]):
            team = team_names[side_idx] if side_idx < len(team_names) else "Unknown"
            for order, li in enumerate(ul.find_all("li", class_=re.compile(r"lineup__player")), 1):
                link = li.find("a")
                if not link:
                    continue
                name = link.get_text(strip=True)
                pos_span = li.find("span", class_=re.compile(r"lineup__pos"))
                pos = pos_span.get_text(strip=True) if pos_span else "?"
                lineups[name] = {
                    "team": team,
                    "batting_order": order,
                    "position": pos,
                    "confirmed": True,
                }

    logger.info(f"RotoWire: {len(lineups)} confirmed lineup spots")
    return lineups
