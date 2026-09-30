# Pre-registered hypotheses for the 2027 MLB season

Locked 2026-09-29, before any 2027 data exists. This file is the protocol. It is
not edited during the season. If a rule turns out to be wrong or unworkable, the
fix is a NEW hypothesis with a new name, evaluated from the date it was added,
and this file's entry is marked withdrawn, not rewritten.

The executable form of these rules is `prereg_2027.py` (read-only; prints
record, Wilson CI, and PASS / FAIL / INCONCLUSIVE). If the script and this
document disagree, this document wins and the script is the bug.

## Common rules (apply to both hypotheses)

- **Breakevens.** Primary: 55.0% (pick'em 3-pick at 6x). Secondary, reported
  but not required: 57.7% (pick'em 2-pick at 3x). A hypothesis PASSES only
  against the primary breakeven; clearing the secondary is noted separately.
- **Significance bar.** Two hypotheses are pre-registered, so the family-wise
  0.05 is split (Bonferroni): one-sided alpha 0.025 per hypothesis. The test is
  "Wilson score interval lower bound (z = 1.96, i.e. the same interval
  `report.wilson_ci` already prints) strictly above the breakeven". The point
  estimate being above breakeven is not a pass.
- **Sample floor.** No verdict is issued before the floor below is reached.
  If the 2027 regular season ends below the floor the verdict is INCONCLUSIVE:
  the hypothesis neither passes nor fails and may be carried into 2028 with the
  2027 plays kept in its sample.
- **Verdict timing.** One verdict per hypothesis, computed after the last
  regular-season game of 2027 is graded, from `python prereg_2027.py --season 2027`.
  The running record may be displayed during the season. It may not be used to
  change the rule, the floor, the breakeven, or what counts as a play.
- **Grading.** Win/loss against the UD Fantasy Points line captured in the
  play's own saved snapshot for that date (`data/ud_under_band_plays/{date}.json`
  or the date's entry in `data/results/top25_results.json`), exactly as
  `tracker.py` grades those cohorts today. Push (actual equals line) and DNP are
  excluded from n. A play is in the sample if it was persisted by the pipeline
  before first pitch; nothing is added or removed by hand.
- **Dates excluded.** Only dates the pipeline itself flags as low coverage
  (`report.LOW_COVERAGE_DATES`), and only if that flag is added for a
  data-quality reason unrelated to the play's outcome.
- **Required-rate table** (observed win rate at which the Wilson lower bound
  clears the breakeven), so nobody has to trust the script:

  | n | > 55.0% | > 57.7% |
  |---|---|---|
  | 100 | 64.8% | 67.3% |
  | 150 | 63.0% | 65.6% |
  | 200 | 61.9% | 64.5% |
  | 250 | 61.2% | 63.8% |
  | 300 | 60.7% | 63.2% |
  | 400 | 59.9% | 62.5% |
  | 500 | 59.4% | 62.0% |

## H1: high-line UNDER slice

**Rule.** A play is every market-anchored hitter row where

1. the UD Fantasy Points line is **7.5 or higher** (`ud_line >= 7.5`), and
2. the projection edge (`projected_ud - ud_line`) is in **(-2.0, -1.5]**, which
   is the existing band `report.in_ud_under_band` (exact -2.0 excluded, exact
   -1.5 included), and
3. the call is UNDER.

No lineup, batting-order, park, opponent, or player-record condition. Membership
is whatever `save_ud_under_band_plays` persisted for that date, filtered by
condition 1. There is no minimum or maximum line above 7.5.

**Why this rule.** In 2026 the band's out-of-sample record was 158-142 (52.7%)
overall, but split by line it was 124-127 (49.4%) at 6.5 and below and 25-12
(67.6%, n=37, CI 51.5-80.4) at 7.5 and above. The 7.5 cutoff is the boundary the
existing `report.band_line_bucket` already used, not one fitted to the result;
every 2026 band play at 7.0 or above was in fact at 7.5 or above, so 7.0 and 7.5
are the same rule on that data.

**Breakeven to clear:** 55.0% (primary). Reported against 57.7% as well.

**Sample floor:** 150 graded plays. 2026 produced 37 in 41 tracked days
(~0.9/day), so a full season should produce roughly 150-180.

**Pass:** Wilson lower bound > 55.0% at n >= 150. **Fail:** n >= 150 and lower
bound <= 55.0%. **Inconclusive:** n < 150 at season end.

**2026 in-sample figure, for the record:** 25-12 (67.6%, n=37). This is a
discovery number and carries no weight in the 2027 verdict.

## H2: track-record filter

**Rule.** A play is every Top-25 row where, using ONLY that player's Top-25
history strictly before the play's date,

1. the player has **at least 15 graded Top-25 decisions** (wins + losses;
   pushes and DNPs don't count) - the existing `report.FIRE_MIN_N`, and
2. the player's **shrunk** Top-25 win rate is **at least 0.55** - the existing
   `report.MARK_MIN_RATE`, where "shrunk" means the Beta-Binomial posterior mean
   in `shrinkage.shrink_rate` with prior strength 20
   (`SHRINKAGE_PRIOR_STRENGTH`) and baseline equal to the pooled Top-25 win
   rate across all players strictly before that date, and
3. the supporting signal is a **market edge of at least +1.0 line points**
   (`projected_ud - ud_line >= 1.0`, the existing `report.TOP25_OVER_EDGE_MIN`),
   and
4. the call is OVER.

Platoon advantage and the contact-percentile heuristic are NOT part of this
rule; they neither qualify nor disqualify a play. Raw win rate is never used.

**History carry-over.** The player record used in conditions 1-2 starts 2027
with each player's full 2026 Top-25 history carried in unchanged. This is stated
now so the first weeks of 2027 are not a different rule from the rest of the
season. If a player has no 2026 history, they qualify only once they reach 15
decisions in 2027.

**Why this rule.** Rank alone was 49.8% on 2,427 plays in 2026. The forward
"fire" cohort (conditions 1-2, no edge requirement, graded from 2026-07-12) went
73-58 (55.7%, n=131). All three constants are the values already in the code
before this document was written; they were not fitted to the number below.

**Breakeven to clear:** 55.0% (primary). Reported against 57.7% as well.

**Sample floor:** 120 graded plays. 2026 produced 83 rule-matching plays over
the ~105 tracked days from 2026-06-14, without the benefit of carried-over
history, so a full season should produce roughly 120-160.

**Pass:** Wilson lower bound > 55.0% at n >= 120. **Fail:** n >= 120 and lower
bound <= 55.0%. **Inconclusive:** n < 120 at season end.

**2026 in-sample figure, for the record:** 53-30 (63.9%, n=83, CI 53.1-73.4)
for the exact rule above, point-in-time. A grid of nearby parameter choices
(n >= 8/15/20, shrunk >= 0.55/0.58, edge >= 0.5/1.0) was looked at on 2026 data
before locking; every cell landed between 60% and 67%, and the pre-existing code
constants were kept rather than the best cell. This number is a discovery number
and carries no weight in the 2027 verdict.

## What a PASS does and does not mean

A PASS means the rule beat the 3-pick breakeven out of sample at the stated
confidence over one season. It does not mean the effect size is the 2026 figure,
and it does not license widening the rule. A FAIL retires the rule as written.
Any variant is a new hypothesis for 2028, pre-registered the same way.

## Prerequisite that is not a hypothesis

Forward CLV capture was rebuilt on 2026-09-29 (`clv.py`, `clv_snapshot.py`, the
"CLV - observe live UD lines" workflow step) after the 2026 record came out 607
pushes in 608 plays. CLV is not part of either verdict above, but if 2027 CLV
data again shows fewer than half of graded plays with a real two-sample capture
(`record.plays_multi_sample`), the season's line data should be treated as
suspect before either verdict is read.
