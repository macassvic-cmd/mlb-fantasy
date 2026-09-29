# MLB Model - 2026 Offseason Report

Generated 2026-09-28 after the final regular-season day (2026-09-27) was graded.
Source of truth: `python offseason_report.py` plus a supplementary split script
(monthly/edge/line splits, Wilson CIs, cohort checks). Read-only; no results file
was modified beyond the normal 2026-09-27 tracker grade.

Breakeven references used throughout: 52.4% (a -110 straight bet), 55.0%
(pick'em 3-pick at 6x), 57.7% (pick'em 2-pick at 3x).

## 0. Sunset actions taken

- GitHub Actions workflow "MLB Fantasy Pipeline" disabled (`gh workflow disable`).
  Its Sep 28 fires were already failing with "No games found". Stale Line Detector
  and Soccer DNS Detector were already disabled. Soccer DNS Intraday/Digest, SGP Play
  Card (NFL) and Betr Access Check were left active because they are not MLB.
- All seven local "MLB *" Windows scheduled tasks were already Disabled.
- Two orphaned elevated python.exe processes (PIDs 3460/7696, started 2026-09-17)
  are still running `dabble_dns_local.py` every 30s and could not be killed from a
  non-elevated shell. Kill from an admin prompt: `taskkill /F /T /PID 3460`.
- 2026-09-27 graded locally with `tracker.py 2026-09-27` and pushed. Code and data
  untouched otherwise.

## 1. UD UNDER 1.5-2.0 edge band

| Window | Record | Rate | n | 95% Wilson CI |
|---|---|---|---|---|
| In-sample seed (through 08-17) | 327-239 | 57.8% | 566 | 53.7-61.8 |
| Out-of-sample live (08-18 .. 09-27) | 158-142 | 52.7% | 300 | 47.0-58.2 |
| Combined | 485-381 | 56.0% | 866 | 52.7-59.3 |

7 DNP excluded. Low-coverage dates 08-27 and 08-28 excluded per `report.LOW_COVERAGE_DATES`.

Out-of-sample by fortnight (08-18 itself, 9-3, is header-only and not in the splits):

| Window | Record | Rate | n |
|---|---|---|---|
| Aug 19-31 | 45-30 | 60.0% | 75 |
| Sep 1-14 | 60-57 | 51.3% | 117 |
| Sep 15-27 | 44-52 | 45.8% | 96 |

By line value (live-tracked only):

| Line | Record | Rate | n | 95% CI |
|---|---|---|---|---|
| 5.5 | 74-81 | 47.7% | 155 | 40.0-55.6 |
| 6.5 | 50-46 | 52.1% | 96 | 42.2-61.8 |
| 7.5 | 16-7 | 69.6% | 23 | 49.1-84.4 |
| 8.5+ | 9-5 | 64.3% | 14 | 38.8-83.7 |
| 7.5+ pooled | 25-12 | 67.6% | 37 | 51.5-80.4 |
| 6.5 and under pooled | 124-127 | 49.4% | 251 | 43.3-55.5 |

By edge sub-band: (-2.0,-1.75] 68-62 (52.3%), (-1.75,-1.5] 81-77 (51.3%). No gradient.

One-sided test of the live record against 50%: z = 0.53, p = 0.30. Against the
55% breakeven: z = -1.17 (below it).

## 2. Forward CLV

Overall 1-0 with 607 pushes (n=608). OVER 1-0-480, UNDER 0-0-127. Avg +0.002 pts.

This is an instrumentation failure, not a finding. `scrapers/market_lines.py`
caches the UD board once per date and only refetches when the cached count is
thin, so every dashboard render after the first re-reads the same `ud_line`.
Open and close are the same fetch by construction. The backtest had two-snapshot
coverage on only 2 of 72 dates. There is no usable CLV data from 2026.

## 3. Top-25: gated (edge + signal) vs ungated (rank alone)

| Cohort | Record | Rate | n | 95% CI |
|---|---|---|---|---|
| Ungated, full season | 1209-1218 | 49.8% | 2427 | 47.8-51.8 |
| Ungated, same window (09-13 .. 09-27) | 191-182 | 51.2% | 373 | 46.1-56.2 |
| Gated, since 09-13 | 92-77 | 54.4% | 169 | 46.9-61.8 |

Gate beat rank alone by +3.2 points in the same window (p vs 50% = 0.14). All 169
gated plays were OVERs. By gate reason:

| Reason | Record | Rate | n |
|---|---|---|---|
| edge + strong contact | 69-66 | 51.1% | 135 |
| edge + platoon advantage | 10-7 | 58.8% | 17 |
| edge + shrunk sample above baseline | 13-4 | 76.5% | 17 |

By edge size: [1.0,1.5) 14-11, [1.5,2.0) 11-9, 2.0+ 67-57 (54.0%). No gradient.

Ungated by month: Jun 48.0%, Jul 49.3%, Aug 51.3%, Sep 49.6%. Tier "yellow" 60-86
(41.1%, n=146) is the only sub-slice clearly away from 50%, in the wrong direction.

## 4. Shrunk per-player leaders (prior strength 20, baseline 49.7%)

| Player | W-L | n | raw | shrunk |
|---|---|---|---|---|
| Seiya Suzuki | 25-8 | 33 | 75.8 | 65.9 |
| TJ Rumfield | 10-2 | 12 | 83.3 | 62.3 |
| Luis Arraez | 11-3 | 14 | 78.6 | 61.6 |
| Jackson Merrill | 20-9 | 29 | 69.0 | 61.1 |
| Steven Kwan | 12-4 | 16 | 75.0 | 60.9 |
| Lawrence Butler | 7-1 | 8 | 87.5 | 60.5 |
| Brice Turang | 16-7 | 23 | 69.6 | 60.3 |
| Gabriel Moreno | 14-6 | 20 | 70.0 | 59.8 |
| Henry Bolte | 8-2 | 10 | 80.0 | 59.8 |
| Randy Arozarena | 14-7 | 21 | 66.7 | 58.4 |
| Francisco Lindor | 8-3 | 11 | 72.7 | 57.8 |
| Otto Lopez | 12-6 | 18 | 66.7 | 57.7 |
| Ceddanne Rafaela | 12-6 | 18 | 66.7 | 57.7 |
| Bryce Harper | 16-9 | 25 | 64.0 | 57.6 |
| CJ Abrams | 18-11 | 29 | 62.1 | 57.0 |

UNDER-band leaders: only Royce Lewis clears n=8 (5-3, shrunk 54.8%).

Forward test of per-player persistence (marked-plays cohorts, graded on the
appearance after the badge was earned):

| Cohort | Record | Rate | n | 95% CI | since |
|---|---|---|---|---|---|
| fire (n>=15, >=55%) | 73-58 | 55.7% | 131 | 47.2-63.9 | 07-12 |
| hot (n>=8, >=55%) | 56-48 | 53.8% | 104 | 44.3-63.1 | 07-03 |

## 5. Retired experiments - final out-of-sample numbers

| Experiment | Backtest | Out-of-sample | n |
|---|---|---|---|
| Premium tier | 67.8% | 42-42 (50.0%) | 84 |
| Strong tier | 64.1% | 199-199 (50.0%) | 398 |
| Slips, all 11 variants (parlay) | 3.93 expected wins | 0-116 (0.0%) | 116 |
| Slips, leg level | - | 353/714 (49.4%) | 714 |
| Stacks legs | - | 234-191 (55.1%) | 425 |
| Stacks trios | - | 32-97 (24.8%) | 129 |
| Stacks full6 | - | 2-55 (3.5%) | 57 |
| Stacks shadow legs/trios/full6 | - | 5-1 / 1-1 / 0-1 | 6 / 2 / 1 |
| 6.5-line non-band UNDER | 54.6% seed | 115-128 (47.3%) | 243 |
| Value Plays, season | - | 349-323 (51.9%) | 672 |

Value Plays by month: Jun 50.0%, Jul 48.6%, Aug 58.7% (n=213), Sep 48.2%.

## 6. Verdict

Nothing cleared a pick'em breakeven out of sample with real volume.

- The UNDER 1.5-2.0 band is the only signal that was ever validated, and it did
  not replicate at production strength. 52.7% live with a CI that spans 47-58,
  sliding from 60% to 46% across three fortnights. Its in-sample 57.8% was a
  discovery number and the live period regressed most of the way to the market.
- The Top-25 gate did what it was designed to do (turn a 51% cohort into a 54%
  one) but 169 plays cannot distinguish 54% from 50%, and 135 of them rode the
  unbacktested "strong contact" heuristic at 51%. The 13-4 "shrunk sample above
  baseline" reason is the one interesting cell, and it is 17 plays.
- Per-player persistence is the most consistent thing in the data: the fire cohort
  ran 55.7% forward for 131 plays, the shrunk leaderboard has several 20+ sample
  names above 60% raw, and the gate reason built on it was the best reason. It is
  still not proven at breakeven (CI floor 47%).
- High-line UNDERs (7.5+) at 67.6% on 37 plays are a hypothesis, not a result.
- Premium/Strong, slips, stacks, and the 6.5 non-band UNDER are dead. Two of them
  regressed to exactly 50.0% and the slips went 0 for 116 against 3.9 expected.

Worth rebuilding around next season: per-player persistence as a first-class
feature (pre-register the fire threshold and the shrunk-above-baseline gate
before Opening Day), and the 7.5+ UNDER slice as a pre-registered test. Fix CLV
capture (close-line refetch at lock) before trusting any 2027 signal, because
without CLV there is no way to tell luck from edge at these sample sizes.

Abandon: Top-25 rank as a signal, the strong-contact percentile heuristic, all
parlay/stack constructions, and the 1.5-2.0 band as an unconditional
"actionable" flag.
