# JSON-driven DNS detector (book-agnostic)

Betr's API is now locked behind real HTTP Basic Auth, so both DNS detectors
(MLB "not in lineup," soccer injury) no longer pull a board themselves.
Instead you supply the board as a JSON file. Nothing about the detection
logic changed — same MLB Stats API lineup checks, same Transfermarkt injury
scraping/name matching, same return-date-vs-fixture filter, same Discord
cards + digest. Only the input source changed, so this works for Betr,
Dabble, or any other book — anything you can export a board from.

## 1. Schema

A file is either a bare array of prop records, or an object:

```json
{"sport": "mlb", "props": [ ... ]}
```

**Required per record:** `player_name`, `team`, `market`, `line`, `event_date`
**Optional:** `opponent`, `odds`, `sport` (only needed if not set at the file
level or inferred from the folder), `league` (soccer only — enables result
grading; without it, detection/alerting/digest still work fine)

`sport` is `"mlb"` or `"soccer"` — or a value that maps to one of those (see
below). It can be set once at the file level, per record (overrides the
file level), or left off entirely if the file is dropped in the `mlb/` or
`soccer/` sub-folder of the watched directory.

`event_date` should be ISO8601, ideally UTC (`"2026-09-10T23:05:00Z"`).

### Field aliases — you probably don't need to reshape your export

The loader checks these names for each canonical field, in order, so most
books' native export field names will just work:

| Canonical | Accepted aliases |
|---|---|
| `player_name` | `player_name`, `player`, `name`, `athlete`, `athlete_name` |
| `team` | `team`, `team_name`, `club` |
| `opponent` | `opponent`, `opp`, `vs`, `opponent_team` |
| `market` | `market`, `stat`, `stat_type`, `prop_type`, `market_type`, `category` |
| `line` | `line`, `value`, `point`, `line_value`, `stat_value` |
| `odds` | `odds`, `price`, `american_odds`, `american` |
| `event_date` | `event_date`, `date`, `game_date`, `commence_time`, `start_time`, `start_time_utc`, `event_time` |
| `sport` | `sport`, `league_sport` |
| `league` | `league`, `competition` |
| `matchup` (optional, soccer) | `game`, `matchup`, `fixture_name`, `event_name` |
| `position` (optional, MLB) | `position`, `pos` |

`sport` and `league` VALUES are also mapped, not just matched literally:
- `sport: "Baseball"` → `mlb`, `sport: "Football"` (association football) →
  `soccer`. Anything else (`"American Football"`, `"Cricket"`, `"Tennis"`,
  ...) passes through lowercased and is cleanly skipped/reported rather
  than erroring — see the mixed-file section below.
- `league`/`competition: "England - Premier League"` → `EPL`, and similarly
  for LaLiga (`LLG`), Ligue 1 (`L1F`), Bundesliga (`BUN`), Serie A (`SEA`),
  MLS. An unrecognized competition (e.g. `"UEFA - Champions League"`) is
  kept as its own uppercased string — it displays fine, it just won't
  resolve for result grading (same as no league at all).

**Soccer team names**: Transfermarkt injury lookups and ESPN grading both
need a club's real name (`"AFC Bournemouth"`), not a short code (`"BOU"`) —
guessing wrong there risks matching the wrong club. If a record has no
`opponent` but does have a `matchup` string (e.g. `"game": "Brentford @ AFC
Bournemouth"`) and the sport is soccer, the loader resolves which side the
`team` value refers to and fills in both the full team name and the
opponent. It only does this when the match is unambiguous; otherwise it
leaves `team` untouched rather than risk the wrong club. MLB team values
are never touched this way — 3-letter codes (`"NYY"`) are already what the
detector expects.

**MLB pitchers are excluded automatically.** The lineup-check this detector
uses only makes sense for hitters — a starting pitcher is never in the
batting lineup at all (universal DH), so that check is guaranteed wrong for
them. A record is treated as a pitcher (and dropped before it ever reaches
the lineup check, reported as `skipped_pitchers` in the run summary rather
than silently lost) if its `position` is `SP`/`RP`/`P`/`CL`, or — if no
`position` field is given at all — if it carries a pitcher-only stat market
(`outs`, `earned-runs`, `hits-allowed`, `walks-allowed`, `win`,
`quality-start`, `hold`, `save`). A bare `strikeouts` market alone isn't
enough signal by itself (batters have a strikeouts prop too) — send
`position` if your book has it, so pitchers with only a strikeouts line
still get caught correctly.

A malformed record raises a loud, specific error (missing field names,
record index, and the record's own content) rather than silently dropping
it — **except** when the loader is run in lenient mode (the default for
both the watched-folder single-sport and mixed-sport paths), where a bad
individual record is skipped and listed in the run summary instead of
aborting the whole file. This matters for a large automated export: one
row with an empty `"team"` shouldn't cost every other valid record in a
10,000-row file.

### Examples

`data/dns_watch/examples/mlb_example.json` and
`data/dns_watch/examples/soccer_example.json` are working samples — copy one,
swap in real data, drop it in the matching `incoming/` sub-folder.

## 2. Watched folder

```
data/dns_watch/
  incoming/mlb/      <- drop single-sport MLB JSON files here
  incoming/soccer/   <- drop single-sport soccer JSON files here
  incoming/          <- drop MIXED-sport JSON files directly here (root, not a sub-folder)
  processed/{mlb,soccer,mixed}/   <- moved here after a successful run
  failed/{mlb,soccer,mixed}/      <- moved here on error, with a sibling .error.txt
```

**Mixed-sport files** (one export covering every sport, not split per
book): drop it in `incoming/` itself, not a sub-folder. Every record must
carry its own `"sport"` field (or alias `"league_sport"`) - `"mlb"` or
`"soccer"` - since there's no folder name to default to. The file is split
by that field: `mlb` records go through `stale_lines.run_poll()`, `soccer`
records go through `soccer_dns.run_scan()`, each exactly as if it had been
the whole file, and any record whose `sport` isn't one of those two is
skipped (reported in the run summary, not treated as an error) - so a
bundled export that also includes sports this project doesn't cover yet
(NBA, NFL, whatever) won't fail the whole file. `league` (EPL, MLS, etc.)
is unrelated to routing - it's an optional, soccer-only field used later
for result grading.

If you already know a file is single-sport, still use the `mlb/` /
`soccer/` sub-folders - `sport` can then be omitted per-record and inferred
from the folder.

Run it:

```
python dns_watch.py            # process whatever's pending, then exit
python dns_watch.py --watch    # persistent loop, checks every 60s (--interval to change)
```

`--once` (the default) is recommended over `--watch` for routine use — no
long-lived process to forget about or leak, in line with this project's past
orphan-poller incidents. Use `--watch` only if you want true drop-and-forget
and are comfortable stopping it yourself when done.

## 3. Output

Unchanged: individual Discord flag cards as records are matched against
lineups/injuries, plus the same compiled digest — same channels, same
formatting.

## 4. Reused, unmodified logic

- `scrapers/mlb_api.py` — lineups, first-pitch timing
- `scrapers/transfermarkt.py` — injury/suspension scraping, expected-return dates
- Name normalization/matching (`scrapers/betr.normalize_name`)
- `stale_lines.py` / `soccer_dns.py` core detection, classification, Discord
  posting — each only gained one optional parameter (`betr_entries=`,
  `board=`) so a pre-loaded board can be passed straight in instead of being
  fetched live.
