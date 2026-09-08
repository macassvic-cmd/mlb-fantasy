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

`sport` is `"mlb"` or `"soccer"`. It can be set once at the file level, per
record (overrides the file level), or left off entirely if the file is
dropped in the `mlb/` or `soccer/` sub-folder of the watched directory.

`event_date` should be ISO8601, ideally UTC (`"2026-09-10T23:05:00Z"`).

### Field aliases — you probably don't need to reshape your export

The loader checks these names for each canonical field, in order, so most
books' native export field names will just work:

| Canonical | Accepted aliases |
|---|---|
| `player_name` | `player_name`, `player`, `name`, `athlete`, `athlete_name` |
| `team` | `team`, `team_name`, `side`, `club` |
| `opponent` | `opponent`, `opp`, `vs`, `opponent_team` |
| `market` | `market`, `stat`, `stat_type`, `prop_type`, `market_type`, `category` |
| `line` | `line`, `value`, `point`, `line_value`, `stat_value` |
| `odds` | `odds`, `price`, `american_odds`, `american` |
| `event_date` | `event_date`, `date`, `game_date`, `commence_time`, `start_time`, `event_time` |
| `sport` | `sport`, `league_sport` |
| `league` | `league`, `competition` |

A malformed record raises a loud, specific error (missing field names,
record index, and the record's own content) rather than silently dropping
it.

### Examples

`data/dns_watch/examples/mlb_example.json` and
`data/dns_watch/examples/soccer_example.json` are working samples — copy one,
swap in real data, drop it in the matching `incoming/` sub-folder.

## 2. Watched folder

```
data/dns_watch/
  incoming/mlb/      <- drop MLB JSON files here
  incoming/soccer/   <- drop soccer JSON files here
  processed/{mlb,soccer}/   <- moved here after a successful run
  failed/{mlb,soccer}/      <- moved here on error, with a sibling .error.txt
```

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
