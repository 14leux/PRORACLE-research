-- PRORACLE P0 capture: final match scores, league countries, and the daily simulated ticket
-- with its arm picks (see docs/TICKETS.md). Everything here is append-only for the runtime
-- role: it can read and insert, never update or delete. No odds, no stakes, no images and no raw
-- provider payload are stored.
--
-- Idempotent: applying this file a second time changes nothing (tables use IF NOT EXISTS,
-- privileges are revoked and re-granted to the same final state, policies are created only when
-- missing).

-- Regulation-time score of a finished fixture, as returned by the provider. One row per fixture:
-- the runtime role cannot update results or fixtures, so scores live in their own table and can be
-- backfilled for fixtures that already have a result.
create table if not exists proracle.match_scores (
    id uuid primary key default gen_random_uuid(),
    fixture_id uuid not null unique references proracle.fixtures(id) on delete restrict,
    home_score smallint not null check (home_score between 0 and 99),
    away_score smallint not null check (away_score between 0 and 99),
    source_name text not null check (length(btrim(source_name)) > 0),
    observed_at timestamptz not null default now(),
    created_at timestamptz not null default now()
);

comment on table proracle.match_scores is
    'Append-only regulation-time scores, one row per fixture. Operator-configured source label; keep only what the operator may retain.';

-- League country as supplied by the provider (a country, or a region such as Africa). One row per
-- competition id, joined to fixtures.competition_id.
create table if not exists proracle.competition_countries (
    competition_id text primary key check (length(btrim(competition_id)) > 0),
    country text not null check (length(btrim(country)) > 0),
    source_name text not null check (length(btrim(source_name)) > 0),
    observed_at timestamptz not null default now(),
    created_at timestamptz not null default now()
);

comment on table proracle.competition_countries is
    'Append-only league country labels keyed by fixtures.competition_id.';

-- One simulated ticket per UTC date and design version: the unique key is what makes a rerun
-- harmless. The seed and pool size are stored so the selection can be recomputed and checked, and
-- fixture_count says how many ticket_fixtures rows a complete ticket has (a write that stopped
-- part-way leaves fewer, and such a ticket is ignored).
create table if not exists proracle.tickets (
    id uuid primary key default gen_random_uuid(),
    ticket_date date not null,
    ticket_kind text not null check (ticket_kind = 'SIMULATED'),
    design_version text not null check (length(btrim(design_version)) > 0),
    seed text not null check (length(btrim(seed)) > 0),
    pool_size integer not null check (pool_size >= 0),
    fixture_count smallint not null check (fixture_count between 1 and 20),
    frozen_at timestamptz not null,
    created_at timestamptz not null default now(),
    unique (ticket_date, ticket_kind, design_version)
);

comment on table proracle.tickets is
    'Append-only daily simulated tickets (at most 20 fixtures each). Frozen before every kickoff on the ticket.';

create table if not exists proracle.ticket_fixtures (
    ticket_id uuid not null references proracle.tickets(id) on delete restrict,
    fixture_id uuid not null references proracle.fixtures(id) on delete restrict,
    position smallint not null check (position between 1 and 20),
    created_at timestamptz not null default now(),
    primary key (ticket_id, fixture_id),
    unique (ticket_id, position)
);

comment on table proracle.ticket_fixtures is
    'Append-only membership of a ticket. position is the rank in the seeded order, 1 to 20.';

-- One pick per ticket fixture and arm. Arm A is the live rating model's earliest pre-kickoff
-- prediction (source_prediction_id); arm B is written only when a rule set is active; arm C is the
-- draw arm. frozen_at must be before kickoff, so a pick can never be recorded after the fact.
create table if not exists proracle.ticket_arm_picks (
    id uuid primary key default gen_random_uuid(),
    ticket_id uuid not null,
    fixture_id uuid not null,
    arm text not null check (arm in ('A', 'B', 'C')),
    arm_version text not null check (length(btrim(arm_version)) > 0),
    pick text not null check (pick in ('HOME_WIN', 'DRAW', 'AWAY_WIN')),
    forced boolean not null default false,
    score numeric(12, 10) check (score is null or score between 0 and 2),
    source_prediction_id uuid references proracle.predictions(id) on delete restrict,
    kickoff_at timestamptz not null,
    frozen_at timestamptz not null,
    created_at timestamptz not null default now(),
    foreign key (ticket_id, fixture_id)
        references proracle.ticket_fixtures (ticket_id, fixture_id) on delete restrict,
    unique (ticket_id, fixture_id, arm),
    check (frozen_at < kickoff_at),
    check (arm <> 'A' or source_prediction_id is not null),
    check (arm = 'C' or not forced)
);

comment on table proracle.ticket_arm_picks is
    'Append-only arm picks, frozen before kickoff. A correction or rerun is never an update.';

create index if not exists ticket_arm_picks_fixture_idx on proracle.ticket_arm_picks (fixture_id);

-- Privileges: revoke everything, then grant exactly select and insert. service_role gets the same
-- access as on the other PRORACLE tables; the restricted runtime role is the day-to-day user.
revoke all privileges on table
    proracle.match_scores,
    proracle.competition_countries,
    proracle.tickets,
    proracle.ticket_fixtures,
    proracle.ticket_arm_picks
    from public, anon, authenticated, service_role, proracle_runtime;

grant select, insert on table
    proracle.match_scores,
    proracle.competition_countries,
    proracle.tickets,
    proracle.ticket_fixtures,
    proracle.ticket_arm_picks
    to service_role, proracle_runtime;

alter table proracle.match_scores enable row level security;
alter table proracle.competition_countries enable row level security;
alter table proracle.tickets enable row level security;
alter table proracle.ticket_fixtures enable row level security;
alter table proracle.ticket_arm_picks enable row level security;

-- service_role bypasses RLS; a custom role does not, so proracle_runtime needs policies. Created
-- only when missing so a second application changes nothing.
do $policies$
declare
    table_name text;
begin
    foreach table_name in array array[
        'match_scores', 'competition_countries', 'tickets', 'ticket_fixtures', 'ticket_arm_picks'
    ] loop
        if not exists (
            select 1 from pg_policies
             where schemaname = 'proracle' and tablename = table_name
               and policyname = 'proracle_runtime_select_' || table_name
        ) then
            execute format(
                'create policy %I on proracle.%I for select to proracle_runtime using (true)',
                'proracle_runtime_select_' || table_name, table_name
            );
        end if;
        if not exists (
            select 1 from pg_policies
             where schemaname = 'proracle' and tablename = table_name
               and policyname = 'proracle_runtime_insert_' || table_name
        ) then
            execute format(
                'create policy %I on proracle.%I for insert to proracle_runtime with check (true)',
                'proracle_runtime_insert_' || table_name, table_name
            );
        end if;
    end loop;
end
$policies$;
