# P0 capture and the daily simulated ticket

Three optional additions that collect evidence going forward. Nothing here changes a prediction, and every step is **off by default**: until you switch it on, the scheduled run behaves exactly as before.

## What is captured

| What | Where | When |
|---|---|---|
| Final regulation-time score of a finished match | `proracle.match_scores` (one row per fixture) | With a result the run adds, when `PRORACLE_CAPTURE_SCORES=1`. Older results by the manual backfill below. |
| League country as the provider labels it (a country, or a region such as `Africa`) | `proracle.competition_countries` (one row per competition id) | Once per run, with the scores switch. Rows already stored are left as they are. |
| A simulated ticket of 20 fixtures with the arms' picks | `proracle.tickets`, `ticket_fixtures`, `ticket_arm_picks` | Once per UTC date, when `PRORACLE_DAILY_TICKET=1` |

All five tables are append-only for the restricted runtime role (read and insert only; [RESTRICTED_ROLE.md](RESTRICTED_ROLE.md)). They hold no odds, stakes, images or raw provider payloads. Apply `supabase/migrations/20261126000000_proracle_p0_capture.sql` first: with a switch on and the tables missing, the run counts the failed writes (and stops trying after five in one run) instead of failing, but nothing is captured.

## The daily simulated ticket

A ticket is a record of what each arm would have picked on a seeded sample of the day's fixtures. It is a measurement tool for later reports, not betting advice, and it uses no odds.

1. **Pool.** Stored fixtures that are still scheduled and kick off in the 24 hours after the run, each with the rating model's earliest pre-kickoff prediction (`elo_davidson_recalibrated`). A pool of fewer than 20 skips the day.
2. **Selection.** The 20 fixtures with the smallest `sha256("proracle-sim-ticket:sim-v1:<UTC date>:<fixture id>")`. This is a hash order, not a random generator, so the same pool and date always give the same ticket on any Python version, and the choice can be recomputed.
3. **Arm A** is the rating model's pick (its most probable outcome; ties resolve home, draw, away) and links to the prediction row it came from.
4. **Arm C** (`draw-score-v1`) forces the five fixtures with the highest draw score to a draw and otherwise follows arm A. The score is the competition's draw rate over all settled history, pulled toward the global draw rate by a prior of 40 matches, plus the rating model's own draw probability for that fixture. Ties go to the lower fixture id. No team-level draw tendency is used: the research found none that persists.
5. **Arm B** is written only when a rule set is active. None exists yet, so tickets hold arms A and C.
6. **Frozen before kickoff.** Every pick carries `frozen_at`, and the database rejects a row whose `frozen_at` is not before its `kickoff_at`. Only fixtures that have not kicked off are in the pool.
7. **One per day.** The unique key (`ticket_date`, `ticket_kind`, `design_version`) means a rerun creates nothing. The ticket id is derived from the date.

The writes are three requests (ticket, its fixtures, its picks), not one transaction. If a later one fails, the ticket row stays with fewer `ticket_fixtures` rows than its `fixture_count`; treat such a ticket as incomplete and ignore it when scoring.

## Switching it on

Set the switch in the runtime's environment to exactly `1` (any other value leaves the step off):

| Variable | Effect |
|---|---|
| `PRORACLE_CAPTURE_SCORES` | Capture scores with new results and the league countries each run. |
| `PRORACLE_DAILY_TICKET` | Build the day's ticket after the predictions. It never raises and never alters a prediction. |

When on, the run summary gains the counters `scores_captured`, `countries_captured`, `capture_errors` and `ticket_created` / `ticket_exists` / `ticket_skipped` / `ticket_errors`.

## Backfilling scores

Scores for results stored before the switch was on are filled in by hand:

```powershell
python -m proracle.score_backfill --days 30
```

This is a dry run: it reads the provider's finished events and counts them. Writing needs both the flag and an explicit environment switch:

```powershell
$env:PRORACLE_ALLOW_SCORE_BACKFILL = "1"
python -m proracle.score_backfill --days 30 --execute
```

`--days` is 1 to 365 (default 30) and is read in 30-day chunks. Fixtures that already have a score are skipped, so it is safe to rerun. The run stops after five failed writes. It needs `BSD_API_TOKEN`, and with `--execute` the same `SUPABASE_*` variables as the main run.

## Checking the migration locally

With Docker running, from the repository root (the same disposable local database as in [DATABASE.md](DATABASE.md)):

```powershell
supabase start
supabase db reset --local
$container = docker ps --filter 'name=supabase_db_proracle' --format '{{.Names}}'
docker cp supabase/tests/verify_p0_capture.sql "${container}:/tmp/verify_p0.sql"
docker exec $container psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres -f /tmp/verify_p0.sql
```

`verify_p0_capture.sql` checks that the runtime role can insert into every new table but cannot update, delete or truncate any of them, that `anon` and `authenticated` see nothing, and that each constraint fires (one ticket per day, frozen before kickoff, arm rules, one score per fixture). It rolls back everything it writes. To check that applying the migration a second time changes nothing, run `supabase/tests/p0_catalog_fingerprint.sql` before and after re-running the migration file; the output must be identical.
