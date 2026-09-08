"""
Book-agnostic prop-board loader. Replaces a live book API (Betr, now
locked behind real HTTP Basic Auth - see stale_lines.py's and
soccer_dns.py's own module docstrings, and data/stale_lines/DISABLED)
with a JSON file the user supplies by hand. Only the SOURCE of "what
props are currently live" changes - the actual detection logic
(Transfermarkt injury matching for soccer, MLB lineup checks for
baseball, the fixture-date-vs-return-date filter, Discord alerting/
digest) is completely unchanged; see to_mlb_betr_entries() and
to_soccer_board() below, which produce EXACTLY the shapes
scrapers.betr.fetch_betr_hitter_lines_with_context() and
soccer_dns.fetch_betr_soccer_board() already produced, so
stale_lines.run_poll() and soccer_dns.run_scan() only needed a one-
parameter change each to accept a pre-loaded board instead of fetching
one themselves.

SCHEMA: a JSON file is either a bare array of prop records, or an
object {"sport": "mlb"|"soccer", "props": [...]}. A per-record "sport"
key overrides the file-level one; if NEITHER is present, the caller
must supply a default via load_prop_records()'s sport_hint (the
watched-folder runner, dns_watch.py, uses the sub-folder name for this
- data/dns_watch/incoming/mlb/ vs .../soccer/).

Required per record: player_name, team, market, line, event_date.
Optional: opponent, odds, sport.

FIELD ALIASES - a record can use any of these key names for each
canonical field (first match wins, checked in the order listed), so
you very likely don't need to reshape whatever your book's export
already looks like:
  player_name : player_name, player, name, athlete, athlete_name
  team        : team, team_name, side, club
  opponent    : opponent, opp, vs, opponent_team
  market      : market, stat, stat_type, prop_type, market_type, category
  line        : line, value, point, line_value, stat_value
  odds        : odds, price, american_odds, american
  event_date  : event_date, date, game_date, commence_time, start_time, event_time
  sport       : sport, league_sport

event_date should be an ISO8601 string (ideally UTC, "2026-09-10T23:05:00Z")
- for MLB this feeds directly into the same first-pitch-distance/
lineup-window logic that used to read Betr's own event_date_utc; for
soccer it's compared against Transfermarkt's expected-return-date the
same way Betr's own event date always was.

Deliberately fails LOUDLY (raises ValueError naming the exact record)
on a malformed entry rather than silently skipping it - a DNS detector
quietly dropping a player because of one bad record in the file is
exactly the kind of failure this project has spent a lot of effort
hunting down elsewhere (see stale_lines_watchdog.py's own history).
"""

import json
import os
from dataclasses import dataclass
from typing import List, Optional

from scrapers.betr import normalize_name

FIELD_ALIASES = {
    "player_name": ["player_name", "player", "name", "athlete", "athlete_name"],
    "team": ["team", "team_name", "side", "club"],
    "opponent": ["opponent", "opp", "vs", "opponent_team"],
    "market": ["market", "stat", "stat_type", "prop_type", "market_type", "category"],
    "line": ["line", "value", "point", "line_value", "stat_value"],
    "odds": ["odds", "price", "american_odds", "american"],
    "event_date": ["event_date", "date", "game_date", "commence_time", "start_time", "event_time"],
    "sport": ["sport", "league_sport"],
    # Optional, soccer-only: one of soccer_dns.LEAGUES ("EPL","MLS","LLG",
    # "L1F","BUN","SEA") - without it, JSON-sourced soccer flags still
    # detect/alert/digest fine, but soccer_dns.py's grading pass can't
    # resolve an ESPN league slug to check the final result (see
    # to_soccer_board()'s own docstring) and those flags stay unresolved.
    "league": ["league", "competition"],
}
REQUIRED_FIELDS = ["player_name", "team", "market", "line", "event_date"]


@dataclass
class PropRecord:
    player_name: str
    normalized_name: str
    team: str
    market: str
    line: float
    event_date: str
    opponent: Optional[str] = None
    odds: Optional[float] = None
    sport: Optional[str] = None
    league: Optional[str] = None
    source_file: Optional[str] = None


def _find_field(record, canonical):
    for alias in FIELD_ALIASES[canonical]:
        if alias in record and record[alias] not in (None, ""):
            return record[alias]
    return None


def _normalize_record(record, sport_hint, source_file, index):
    missing = [f for f in REQUIRED_FIELDS if _find_field(record, f) is None]
    if missing:
        raise ValueError(f"{source_file} record #{index}: missing required field(s) {missing} "
                          f"(checked aliases {[FIELD_ALIASES[m] for m in missing]}) - record was {record!r}")

    sport = _find_field(record, "sport") or sport_hint
    if not sport:
        raise ValueError(f"{source_file} record #{index}: no 'sport' field on the record or the file, "
                          f"and no folder-based default available - add \"sport\": \"mlb\"|\"soccer\" "
                          f"to the record/file, or drop it in a sport-named sub-folder.")

    raw_line = _find_field(record, "line")
    try:
        line_val = float(raw_line)
    except (TypeError, ValueError):
        raise ValueError(f"{source_file} record #{index}: 'line' must be numeric, got {raw_line!r}")

    odds_raw = _find_field(record, "odds")
    odds_val = None
    if odds_raw is not None:
        try:
            odds_val = float(odds_raw)
        except (TypeError, ValueError):
            pass  # odds is cosmetic/optional here - don't fail the whole record over it

    player_name = str(_find_field(record, "player_name")).strip()
    opponent_raw = _find_field(record, "opponent")
    league_raw = _find_field(record, "league")

    return PropRecord(
        player_name=player_name,
        normalized_name=normalize_name(player_name),
        team=str(_find_field(record, "team")).strip(),
        market=str(_find_field(record, "market")).strip().upper(),
        line=line_val,
        event_date=str(_find_field(record, "event_date")).strip(),
        opponent=str(opponent_raw).strip() if opponent_raw else None,
        odds=odds_val,
        sport=str(sport).strip().lower(),
        league=str(league_raw).strip().upper() if league_raw else None,
        source_file=source_file,
    )


def load_prop_records(source, sport_hint=None):
    """source: a file path (str/PathLike) OR an already-parsed dict/list
    (for tests or programmatic use, no file I/O needed)."""
    if isinstance(source, (str, os.PathLike)):
        source_file = str(source)
        with open(source, encoding="utf-8") as f:
            data = json.load(f)
    else:
        source_file = "<in-memory>"
        data = source

    if isinstance(data, dict):
        file_sport = data.get("sport")
        props = data.get("props")
        if props is None:
            raise ValueError(f"{source_file}: object input must have a 'props' array.")
    elif isinstance(data, list):
        file_sport = None
        props = data
    else:
        raise ValueError(f"{source_file}: top-level JSON must be an array or an object with a 'props' array.")

    effective_hint = file_sport or sport_hint
    records = []
    for i, raw in enumerate(props):
        if not isinstance(raw, dict):
            raise ValueError(f"{source_file} record #{i}: expected an object, got {type(raw).__name__}.")
        records.append(_normalize_record(raw, effective_hint, source_file, i))
    return records


def to_mlb_betr_entries(records):
    """[{name, normalized_name, team, event_date_utc, markets: {stat_key: line}}]
    - matches scrapers.betr.fetch_betr_hitter_lines_with_context()'s
    exact return shape (one entry per player, markets merged across
    that player's multiple prop records in the file)."""
    by_name = {}
    for r in records:
        if r.sport != "mlb":
            continue
        entry = by_name.setdefault(r.normalized_name, {
            "name": r.player_name, "normalized_name": r.normalized_name,
            "team": r.team, "event_date_utc": r.event_date, "markets": {},
        })
        entry["markets"][r.market] = r.line
    return list(by_name.values())


def to_soccer_board(records):
    """{event_key: {"date":, "league": None, "teams": {team: {norm_name: {"name":, "markets": {stat_key: line}}}}}}
    - matches soccer_dns.fetch_betr_soccer_board()'s shape (a dict of
    market->line VALUES rather than Betr's fetcher's set-of-keys - a
    strict superset for every existing consumer, since sorted()/`in`
    over a dict's keys behaves identically to a set of those same keys,
    so this doesn't require touching soccer_dns.py's digest/embed code).

    event_key is synthesized from (team, opponent, event_date's date
    part) since a manually-supplied JSON has no equivalent to Betr's own
    internal event ids - team/opponent are sorted so both sides of the
    same fixture land under the same key regardless of which one a
    given record lists as "team" vs "opponent". "league" comes from the
    record's own optional "league" field (one of soccer_dns.LEAGUES) if
    given, else None - soccer_dns.py's grading pass needs a real league
    to resolve the correct ESPN league slug, so grading won't run for a
    JSON-sourced flag with no league specified (detection/alerting/
    digest are unaffected either way - none of them read this field,
    and _flag_embed() shows "Unknown" rather than erroring on None -
    found live 2026-09-08, a bare None there 400'd Discord's embed API).
    """
    events = {}
    for r in records:
        if r.sport != "soccer":
            continue
        date_part = r.event_date[:10]
        pair = tuple(sorted([r.team, r.opponent or ""]))
        event_key = f"{pair[0]}__{pair[1]}__{date_part}"

        ev = events.setdefault(event_key, {"date": r.event_date, "league": r.league, "teams": {}})
        if r.league and not ev.get("league"):
            ev["league"] = r.league
        if r.opponent:
            # Register the opponent as a (possibly empty) side too, even
            # if this file has no props for any of their players - a
            # sparse JSON that only lists props for one team otherwise
            # leaves ev["teams"] with a single key, and soccer_dns.py's
            # `other_team = next(t for t in ev["teams"] if t != team_name)`
            # resolves to None, leaving flag["opponent"] blank (breaks
            # the digest's "vs Opponent" line and grading's need for
            # both team names) - found live 2026-09-08.
            ev["teams"].setdefault(r.opponent, {})
        team_bucket = ev["teams"].setdefault(r.team, {})
        entry = team_bucket.setdefault(r.normalized_name, {"name": r.player_name, "markets": {}})
        entry["markets"][r.market] = r.line
    return events
