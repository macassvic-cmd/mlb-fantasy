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
  team        : team, team_name, club
  opponent    : opponent, opp, vs, opponent_team
  market      : market, stat, stat_type, prop_type, market_type, category
  line        : line, value, point, line_value, stat_value
  odds        : odds, price, american_odds, american
  event_date  : event_date, date, game_date, commence_time, start_time,
                start_time_utc, event_time
  sport       : sport, league_sport
  matchup     : game, matchup, fixture_name, event_name (optional - see below)

event_date should be an ISO8601 string (ideally UTC, "2026-09-10T23:05:00Z")
- for MLB this feeds directly into the same first-pitch-distance/
lineup-window logic that used to read Betr's own event_date_utc; for
soccer it's compared against Transfermarkt's expected-return-date the
same way Betr's own event date always was.

SPORT/LEAGUE VALUES aren't just matched literally - a "sport" of
"Baseball" or "Football" (association football) is recognized as "mlb"/
"soccer" the same as those exact words (see SPORT_VALUE_ALIASES; a
sport with no recognized mapping passes through lowercased as-is, so
it's still cleanly skipped/reported rather than crashing - see
dns_watch.py's process_mixed_file()). Likewise a soccer "league"/
"competition" of "England - Premier League" etc. is mapped to the
short code (EPL/LLG/L1F/BUN/SEA/MLS) soccer_dns.py's grading pass
actually needs (see LEAGUE_VALUE_ALIASES) - an unrecognized league
string is kept as-is rather than dropped, so it still displays
correctly, it just won't resolve for grading.

SOCCER TEAM NAMES - Transfermarkt injury lookups and ESPN grading both
need a club's real full name ("AFC Bournemouth"), not a book's short
code ("BOU") - passing a short code straight to Transfermarkt's search
risks matching the WRONG club rather than failing safely. If a record
has no explicit "opponent" but does have a "matchup" field (e.g. "game":
"Brentford @ AFC Bournemouth") and the sport is soccer, the loader tries
to resolve which of the two full names the given "team" value refers to
(a same-normalized substring match) and fills in both the full team
name and the opponent from that - see _resolve_soccer_team_name(). Only
applied when the match is unambiguous; otherwise the raw "team" value is
left untouched (no opponent guessed) rather than risking a wrong club.

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
    # "side" deliberately excluded here - one real export uses it for the
    # bet direction ("over"/"under"), not a team designation; "team" itself
    # is always checked first so this only matters if a file has no "team"
    # key at all, and guessing wrong there (over/under as a team name) is
    # worse than just failing the required-field check loudly.
    "team": ["team", "team_name", "club"],
    "opponent": ["opponent", "opp", "vs", "opponent_team"],
    "market": ["market", "stat", "stat_type", "prop_type", "market_type", "category"],
    "line": ["line", "value", "point", "line_value", "stat_value"],
    "odds": ["odds", "price", "american_odds", "american"],
    "event_date": ["event_date", "date", "game_date", "commence_time", "start_time",
                   "start_time_utc", "event_time"],
    "sport": ["sport", "league_sport"],
    # Optional, soccer-only: one of soccer_dns.LEAGUES ("EPL","MLS","LLG",
    # "L1F","BUN","SEA") - without it, JSON-sourced soccer flags still
    # detect/alert/digest fine, but soccer_dns.py's grading pass can't
    # resolve an ESPN league slug to check the final result (see
    # to_soccer_board()'s own docstring) and those flags stay unresolved.
    "league": ["league", "competition"],
    # Optional, soccer-only: a free-text "Away @ Home" (or "vs"/"-")
    # matchup string, used only to derive a full team/opponent name when
    # "team" is a short code and no explicit "opponent" is given - see
    # _resolve_soccer_team_name().
    "matchup": ["game", "matchup", "fixture_name", "event_name"],
}
REQUIRED_FIELDS = ["player_name", "team", "market", "line", "event_date"]

# A record's "sport" value is matched case-insensitively against these
# aliases before falling back to the lowercased raw value as-is - lets a
# book's own sport taxonomy ("Baseball", "Football") route correctly
# without forcing every export to say "mlb"/"soccer" literally. Anything
# that doesn't map here (e.g. "American Football", "Cricket") just passes
# through lowercased, which to_mlb_betr_entries()/to_soccer_board() and
# dns_watch.py's process_mixed_file() all already treat as "not ours" -
# reported/skipped, never an error.
SPORT_VALUE_ALIASES = {
    "mlb": {"mlb", "baseball"},
    "soccer": {"soccer", "football", "association football", "futbol"},
}
_SPORT_VALUE_TO_CANONICAL = {alias: canon for canon, aliases in SPORT_VALUE_ALIASES.items() for alias in aliases}

# Same idea for a soccer record's "league"/"competition" value, mapped to
# the exact codes soccer_dns.LEAGUES / scrapers.espn_soccer.LEAGUE_SLUGS
# expect. A competition with no mapping here (e.g. "UEFA - Champions
# League", which has no single domestic ESPN slug) is kept as its own
# uppercased string rather than dropped - digest/embed display it as-is,
# it just won't resolve for grading (same graceful no-op as a missing
# league entirely).
LEAGUE_VALUE_ALIASES = {
    "EPL": {"epl", "england - premier league", "english premier league", "premier league"},
    "LLG": {"llg", "spain - laliga", "spanish la liga", "la liga", "laliga"},
    "L1F": {"l1f", "france - ligue 1", "ligue 1", "ligue1"},
    "BUN": {"bun", "germany - bundesliga", "bundesliga"},
    "SEA": {"sea", "italy - serie a", "serie a"},
    "MLS": {"mls", "usa - major league soccer", "us - major league soccer", "major league soccer"},
}
_LEAGUE_VALUE_TO_CANONICAL = {alias: canon for canon, aliases in LEAGUE_VALUE_ALIASES.items() for alias in aliases}


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


_MATCHUP_SEPARATORS = [" @ ", " vs. ", " vs ", " v ", " - "]


def _split_matchup(text):
    """"Away @ Home" (or "vs"/"-") -> (side_a, side_b), or None if the
    string doesn't split cleanly into exactly two non-empty names."""
    for sep in _MATCHUP_SEPARATORS:
        if sep in text:
            parts = [p.strip() for p in text.split(sep)]
            if len(parts) == 2 and all(parts):
                return parts[0], parts[1]
    return None


def _normalize_for_match(s):
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _resolve_soccer_team_name(raw_team, matchup_parts):
    """Best-effort (full_team_name, full_opponent_name) from a "team"
    value that might be a short code ("BOU") plus a "Away @ Home"-style
    matchup string ("Brentford @ AFC Bournemouth") - resolves via
    normalized substring containment (works for both a short code like
    "BOU" -> "AFC Bournemouth" and a team value that's already the full
    name). Returns (raw_team, None) unchanged if zero or more than one
    side matches - an ambiguous or failed match is safer than guessing
    the wrong club (Transfermarkt/ESPN lookups key off this name)."""
    side_a, side_b = matchup_parts
    raw_norm = _normalize_for_match(raw_team)
    candidates = [side for side in (side_a, side_b) if raw_norm and raw_norm in _normalize_for_match(side)]
    if len(candidates) != 1:
        return raw_team, None
    team_full = candidates[0]
    opponent_full = side_b if team_full == side_a else side_a
    return team_full, opponent_full


def _normalize_record(record, sport_hint, source_file, index):
    missing = [f for f in REQUIRED_FIELDS if _find_field(record, f) is None]
    if missing:
        raise ValueError(f"{source_file} record #{index}: missing required field(s) {missing} "
                          f"(checked aliases {[FIELD_ALIASES[m] for m in missing]}) - record was {record!r}")

    sport_raw = _find_field(record, "sport") or sport_hint
    if not sport_raw:
        raise ValueError(f"{source_file} record #{index}: no 'sport' field on the record or the file, "
                          f"and no folder-based default available - add \"sport\": \"mlb\"|\"soccer\" "
                          f"to the record/file, or drop it in a sport-named sub-folder.")
    sport_norm = str(sport_raw).strip().lower()
    sport = _SPORT_VALUE_TO_CANONICAL.get(sport_norm, sport_norm)

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
    team_raw = str(_find_field(record, "team")).strip()
    opponent_raw = _find_field(record, "opponent")
    league_raw = _find_field(record, "league")
    matchup_raw = _find_field(record, "matchup")

    team_final = team_raw
    opponent_final = str(opponent_raw).strip() if opponent_raw else None
    if sport == "soccer" and not opponent_final and matchup_raw:
        parts = _split_matchup(str(matchup_raw))
        if parts:
            team_final, opponent_final = _resolve_soccer_team_name(team_raw, parts)

    league_final = None
    if league_raw:
        league_norm = str(league_raw).strip().lower()
        league_final = _LEAGUE_VALUE_TO_CANONICAL.get(league_norm, str(league_raw).strip().upper())

    return PropRecord(
        player_name=player_name,
        normalized_name=normalize_name(player_name),
        team=team_final,
        market=str(_find_field(record, "market")).strip().upper(),
        line=line_val,
        event_date=str(_find_field(record, "event_date")).strip(),
        opponent=opponent_final,
        odds=odds_val,
        sport=sport,
        league=league_final,
        source_file=source_file,
    )


def load_prop_records(source, sport_hint=None, skip_invalid=False):
    """source: a file path (str/PathLike) OR an already-parsed dict/list
    (for tests or programmatic use, no file I/O needed).

    skip_invalid=False (default): a single malformed record raises
    ValueError and aborts the whole load - the original "fail loudly"
    behavior, right for a small hand-built file where a bad record
    means a real schema mismatch worth stopping for. Returns a plain
    list of PropRecord, as always.

    skip_invalid=True: a malformed record is skipped and collected
    instead of aborting the load - for a large automated export (found
    live 2026-09-08: a 10,704-record multi-sport dump with a handful of
    records carrying "team": "" for an obscure player) where one bad row
    otherwise sinks every other valid record in the file, which defeats
    the point. Returns (records, skipped) instead of a bare list -
    skipped is [{"index":, "error":}, ...] - so a caller can still
    surface what got dropped rather than losing it silently."""
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
    skipped = []
    for i, raw in enumerate(props):
        try:
            if not isinstance(raw, dict):
                raise ValueError(f"{source_file} record #{i}: expected an object, got {type(raw).__name__}.")
            records.append(_normalize_record(raw, effective_hint, source_file, i))
        except ValueError as e:
            if not skip_invalid:
                raise
            skipped.append({"index": i, "error": str(e)})

    if skip_invalid:
        return records, skipped
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
