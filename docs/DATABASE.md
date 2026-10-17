# Local Supabase database

PRORACLE's migrations create a dedicated `proracle` schema and four installation-local tables inside it. This keeps the names separate from existing applications if you intentionally share a PostgreSQL database:

- `fixtures` stores the operator's football fixture metadata.
- `predictions` stores immutable 1X2 probability snapshots.
- `results` stores final, abandoned, cancelled, or void result records. A correction is a new row linked to the row it supersedes.
- `prediction_failures` stores allowlisted, append-only failure metadata. It has no free-form message or raw provider/model response field.

The migrations do not store odds, stakes, bankroll activity, raw provider responses, or feature snapshots. Store provider identifiers only when the operator's source terms permit it. `input_snapshot_hash` is an installation-local audit field; do not publish it without considering whether the underlying inputs could be guessed.

## Start a disposable local database

Install the [Supabase CLI](https://supabase.com/docs/guides/local-development/cli/getting-started) and Docker Desktop, then from the repository root:

```powershell
supabase start
supabase db reset --local
$container = docker ps --filter 'name=supabase_db_proracle' --format '{{.Names}}'
docker cp supabase/tests/verify_prediction_schema.sql "${container}:/tmp/proracle_verify.sql"
docker exec $container psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres -f /tmp/proracle_verify.sql
supabase db advisors --local --type security --level info
```

`db reset --local` recreates the local database and applies the checked-in migrations, including the B4 migration. It deletes data in that local database. Keep these commands local; do not add `--linked`, `--db-url`, or `--project-ref` when running this verification. Never run the reset against a hosted project.

For your own hosted Supabase project, apply the checked-in migrations using the documented migration workflow, then add `proracle` to the project's **API → Exposed schemas** setting. The local CLI config already includes it. The migration grants schema usage and table access only to `service_role`; it does not grant schema or table access to `anon` or `authenticated`. Set `PRORACLE_DB_SCHEMA=proracle` in the trusted runtime. The REST adapter sends the schema profile on each request.

Run the verifier with `psql` inside the local database container; `supabase db query --file` sends a file as one prepared query and cannot execute its multi-statement transaction. The verification script rolls back its synthetic fixture, prediction, and result rows. It checks that:

- RLS is enabled on all four PRORACLE tables, and only `service_role` has usage on the `proracle` schema.
- `anon` and `authenticated` cannot read or write the records.
- `service_role` can read and insert; it can update only fixture kickoff and lifecycle status.
- `service_role` cannot update/delete predictions or results, truncate tables, or make other fixture changes.
- Prediction probability and prematch-time constraints hold, final results require an outcome, and each result version has at most one successor.

The server-side service key is required only by an operator's own trusted runtime. Never put it in a browser, public issue, example file, or pull-request workflow.

## Access model

RLS is enabled with no end-user policies because this first release is a single-operator, server-side installation. Supabase's [Data API exposure change](https://supabase.com/changelog/45329-breaking-change-tables-not-exposed-to-data-and-graphql-api-automatically) made non-exposure the default for new projects on 2026-05-30 and is scheduled to apply to all existing projects on 2026-10-30. PRORACLE's minimal local config does not set an automatic-exposure option; each migration instead revokes existing privileges on the tables it creates before granting back the exact service access. It does not alter schema-wide defaults, which could affect unrelated tables in the operator's project. A narrow `GRANT` alone would not remove privileges already present on a table.

**Historical verification record (2026-10-07, before schema isolation):** The original migrations were applied from a blank local database using Supabase CLI 2.117.0 and PostgreSQL 17.6. The SQL verifier and direct Data API checks passed for the then-current `public` schema. This record does not verify the current isolated-schema baseline; the current migration files were amended before first release to isolate the PRORACLE tables in their own schema.

The historical local Security Advisor returned three INFO findings (`RLS Enabled No Policy`), one for each then-existing table. On 2026-10-15, it returned four INFO findings for the current four-table `proracle` schema and no warnings or errors. These INFO findings are expected for the single-operator design: the Data API roles have no grants or policies, and the trusted server-side `service_role` bypasses RLS. Any later client-facing or multi-user access model must add and verify row-ownership policies before granting client roles.

Stop the local containers when finished with `supabase stop`. This preserves the local database; `supabase db reset --local` recreates it and deletes its local contents.

Predictions and results are append-only for the runtime role. Schedule corrections can update only `fixtures.kickoff_at` and `fixtures.status`. A prediction rerun creates a new prediction row. A corrected result creates a new result row with `supersedes_result_id` set to the previous result record. The database owner can still make administrative changes; keep owner credentials private and use migrations for schema changes.

The SQL verification checks all four PRORACLE tables, schema/table privileges, source-key uniqueness, safe failure-event constraints, append-only grants, and coexistence with same-named legacy tables in `public`. The current migration, rollback-only verifier, Data API smoke check, Security Advisor review, and full 84-test offline suite passed on 2026-10-15 in a fresh, isolated local Supabase project. The only smoke-test row was removed when that temporary database volume was deleted. No hosted or operator-owned deployment has been checked. Review the Supabase Security Advisor after applying the migrations to an operator-owned project.
