# archive/ - retired code, kept for history

Nothing in here runs. Files were moved with `git mv` so `git log --follow` still
works. The GitHub workflows that drove them stay in `.github/workflows/` in a
disabled state (`gh workflow list --all`) as the record of what ran.

| Folder | What | Retired |
|---|---|---|
| `soccer/` | Soccer DNS detector, alerts, daily digest, dashboard, scheduler, Rotowire/Transfermarkt/ESPN soccer plumbing, their tests, local launchers | 2026-09-29 |
| `sgp/` | SGP Play Card: Stack Forge pricer, play card renderer, OddsBlaze client, slash-command bot, their tests | 2026-09-29 |
| `betr/` | Betr access check (once-daily probe of the locked Betr GraphQL API) | 2026-09-29 |

Left in place on purpose because live code still imports them:
- `scrapers/espn_soccer.py` - `scrapers/espn_pro_leagues.py` (NFL/NBA/NHL) imports `normalize_name` from it.
- `scrapers/transfermarkt.py` - `watchlist_dns.py` imports `get_team_injuries_cached`.
- `scrapers/betr.py` - shared by `report.py`, `stale_lines.py`, `board_loader.py` and others.
- `discord_health.py` - shared Discord webhook health tracker.
- `dns_watch.py` still has a soccer branch that does `import soccer_dns` lazily; it only fails if a soccer board is dropped into `data/dns_watch/incoming/`.

Data under `data/` and the published pages under `docs/` were not moved.
The devigger (`devigger/`) is NOT retired and is untouched.
