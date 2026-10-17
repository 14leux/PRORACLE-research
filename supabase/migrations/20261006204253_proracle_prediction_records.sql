-- PRORACLE v1: local fixtures, immutable prediction snapshots, and versioned
-- result records. No odds, stake, bankroll, raw provider payload, or feature
-- snapshot is persisted by this migration.

create schema if not exists proracle;
revoke all privileges on schema proracle from public, anon, authenticated;
grant usage on schema proracle to service_role;

create table proracle.fixtures (
    id uuid primary key default gen_random_uuid(),
    sport text not null default 'football' check (sport = 'football'),
    competition_id text,
    competition_name text,
    home_team text not null check (length(btrim(home_team)) > 0),
    away_team text not null check (length(btrim(away_team)) > 0),
    kickoff_at timestamptz not null,
    status text not null default 'SCHEDULED'
        check (status in ('SCHEDULED', 'LIVE', 'COMPLETED', 'POSTPONED', 'CANCELLED', 'ABANDONED', 'UNKNOWN')),
    source_name text,
    source_event_key text,
    created_at timestamptz not null default now(),
    check (home_team <> away_team)
);

comment on table proracle.fixtures is
    'Installation-local fixture metadata. Source identifiers may be stored only where the operator has permission.';
comment on column proracle.fixtures.source_event_key is
    'Installation-local upstream identifier; not a public identifier or a redistribution right.';

create table proracle.predictions (
    id uuid primary key,
    fixture_id uuid not null references proracle.fixtures(id) on delete restrict,
    contract_version text not null check (contract_version = '1.0'),
    predicted_at timestamptz not null,
    kickoff_at timestamptz not null,
    model_provider text not null check (length(btrim(model_provider)) > 0),
    model_name text not null check (length(btrim(model_name)) > 0),
    model_version text not null check (length(btrim(model_version)) > 0),
    prompt_version text not null check (length(btrim(prompt_version)) > 0),
    input_schema_version text not null check (length(btrim(input_schema_version)) > 0),
    input_snapshot_hash text not null check (input_snapshot_hash ~ '^[0-9a-f]{64}$'),
    home_win_probability numeric(12, 10) not null check (home_win_probability between 0 and 1),
    draw_probability numeric(12, 10) not null check (draw_probability between 0 and 1),
    away_win_probability numeric(12, 10) not null check (away_win_probability between 0 and 1),
    created_at timestamptz not null default now(),
    check (predicted_at < kickoff_at),
    check (abs(home_win_probability + draw_probability + away_win_probability - 1) <= 0.000001)
);

comment on table proracle.predictions is
    'Append-only prematch probability records. A correction or rerun is a new row.';
comment on column proracle.predictions.input_snapshot_hash is
    'Installation-local integrity value; do not publish without reviewing whether inputs can be guessed.';

create index predictions_fixture_predicted_at_idx
    on proracle.predictions (fixture_id, predicted_at desc);

create table proracle.results (
    id uuid primary key default gen_random_uuid(),
    fixture_id uuid not null references proracle.fixtures(id) on delete restrict,
    supersedes_result_id uuid,
    status text not null check (status in ('FINAL', 'ABANDONED', 'CANCELLED', 'VOID')),
    actual_outcome text,
    observed_at timestamptz not null default now(),
    finalized_at timestamptz,
    source_name text not null check (length(btrim(source_name)) > 0),
    source_retrieved_at timestamptz,
    created_at timestamptz not null default now(),
    unique (fixture_id, id),
    foreign key (fixture_id, supersedes_result_id)
        references proracle.results (fixture_id, id)
        on delete restrict,
    check (supersedes_result_id is null or supersedes_result_id <> id),
    check (
        (status = 'FINAL'
            and actual_outcome is not null
            and actual_outcome in ('HOME_WIN', 'DRAW', 'AWAY_WIN')
            and finalized_at is not null)
        or
        (status <> 'FINAL'
            and actual_outcome is null
            and finalized_at is null)
    )
);

comment on table proracle.results is
    'Append-only, versioned result observations. Corrections create a new row linked to the prior row.';
comment on column proracle.results.source_name is
    'Operator-configured source label; use a value the operator is permitted to retain.';

create unique index results_one_successor_per_version_idx
    on proracle.results (supersedes_result_id)
    where supersedes_result_id is not null;
create index results_fixture_observed_at_idx
    on proracle.results (fixture_id, observed_at desc);

-- Supabase projects may apply broad grants to a table at creation time.
-- Revoke privileges on these PRORACLE tables before granting the runtime's
-- exact access. Do not change defaults for unrelated tables in proracle.
revoke all privileges on table proracle.fixtures, proracle.predictions, proracle.results
    from public, anon, authenticated, service_role;

grant select, insert on table proracle.fixtures to service_role;
grant update (kickoff_at, status) on table proracle.fixtures to service_role;
grant select, insert on table proracle.predictions to service_role;
grant select, insert on table proracle.results to service_role;

alter table proracle.fixtures enable row level security;
alter table proracle.predictions enable row level security;
alter table proracle.results enable row level security;
