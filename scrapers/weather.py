"""
Weather data via OpenWeatherMap free tier.
Falls back to neutral defaults when key is missing or API fails.
"""

import os
import logging
import requests
from dotenv import load_dotenv

from scrapers._timeout import call_with_timeout

load_dotenv()
logger = logging.getLogger(__name__)

API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
OWM_URL = "https://api.openweathermap.org/data/2.5/forecast"

# Retractable roofs / fully indoor parks — weather irrelevant
INDOOR = {
    "Minute Maid Park", "Tropicana Field", "Chase Field",
    "Rogers Centre", "Globe Life Field", "loanDepot park",
    "American Family Field", "T-Mobile Park",
}

# (lat, lon) for every MLB stadium
STADIUM_COORDS = {
    "Truist Park":                   (33.8907, -84.4677),
    "Oriole Park at Camden Yards":   (39.2839, -76.6216),
    "Fenway Park":                   (42.3467, -71.0972),
    "Guaranteed Rate Field":         (41.8299, -87.6338),
    "Great American Ball Park":      (39.0974, -84.5061),
    "Progressive Field":             (41.4962, -81.6852),
    "Coors Field":                   (39.7559, -104.9942),
    "Comerica Park":                 (42.3390, -83.0485),
    "Minute Maid Park":              (29.7573, -95.3555),
    "Kauffman Stadium":              (39.0517, -94.4803),
    "Angel Stadium":                 (33.8003, -117.8827),
    "Dodger Stadium":                (34.0739, -118.2400),
    "loanDepot park":                (25.7781, -80.2197),
    "American Family Field":         (43.0280, -87.9712),
    "Target Field":                  (44.9817, -93.2781),
    "Citi Field":                    (40.7571, -73.8458),
    "Yankee Stadium":                (40.8296, -73.9262),
    "Oakland Coliseum":              (37.7516, -122.2005),
    "Citizens Bank Park":            (39.9061, -75.1665),
    "PNC Park":                      (40.4469, -80.0057),
    "Petco Park":                    (32.7076, -117.1570),
    "Oracle Park":                   (37.7786, -122.3893),
    "T-Mobile Park":                 (47.5914, -122.3323),
    "Busch Stadium":                 (38.6226, -90.1928),
    "Tropicana Field":               (27.7682, -82.6534),
    "Globe Life Field":              (32.7473, -97.0831),
    "Rogers Centre":                 (43.6414, -79.3894),
    "Nationals Park":                (38.8730, -77.0074),
    "Chase Field":                   (33.4455, -112.0667),
    "Wrigley Field":                 (41.9484, -87.6553),
    "Sutter Health Park":            (38.5811, -121.5000),
}

_DIRS = ["N","NNE","NE","ENE","E","ESE","SE","SSE",
         "S","SSW","SW","WSW","W","WNW","NW","NNW"]

def _deg_to_dir(deg):
    return _DIRS[round(deg / 22.5) % 16]

def _default(venue_name=""):
    return {
        "temp_f": 72.0,
        "wind_speed_mph": 5.0,
        "wind_direction": "N",
        "precip_probability": 0,
        "humidity": 50,
        "condition": "unavailable",
        "is_indoor": venue_name in INDOOR,
        "weather_available": False,
    }

def _coords(venue_name):
    if venue_name in STADIUM_COORDS:
        return STADIUM_COORDS[venue_name]
    for name, c in STADIUM_COORDS.items():
        if venue_name and (venue_name.lower() in name.lower() or name.lower() in venue_name.lower()):
            return c
    return None


def get_stadium_weather(venue_name):
    is_indoor = venue_name in INDOOR

    if not API_KEY or API_KEY == "your_key_here":
        logger.info(f"No OWM key — returning default weather for {venue_name}")
        return _default(venue_name)

    coords = _coords(venue_name)
    if not coords:
        logger.warning(f"No coords for venue: {venue_name}")
        return _default(venue_name)

    lat, lon = coords
    r = call_with_timeout(
        requests.get, OWM_URL,
        params={"lat": lat, "lon": lon, "appid": API_KEY, "units": "imperial", "cnt": 4},
        timeout=10,  # requests' own HTTP-level timeout
        timeout_s=60,  # hard wall-clock backstop in case that doesn't fire
        label=f"OWM request({venue_name})",
    )
    if r is None:
        return _default(venue_name)
    try:
        r.raise_for_status()
        forecasts = r.json().get("list", [])
        f = forecasts[0] if forecasts else {}
        return {
            "temp_f":           round(f.get("main", {}).get("temp", 72), 1),
            "wind_speed_mph":   round(f.get("wind", {}).get("speed", 5), 1),
            "wind_direction":   _deg_to_dir(f.get("wind", {}).get("deg", 0)),
            "precip_probability": round(f.get("pop", 0) * 100),
            "humidity":         f.get("main", {}).get("humidity", 50),
            "condition":        (f.get("weather") or [{}])[0].get("description", ""),
            "is_indoor":        is_indoor,
            "weather_available": True,
        }
    except Exception as e:
        logger.warning(f"OWM request failed for {venue_name}: {e}")
        return _default(venue_name)
