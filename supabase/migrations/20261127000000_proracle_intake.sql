-- PRORACLE Telegram intake (see docs/INTAKE.md): the operator's own chat sends a fixtures
-- screenshot or PDF; a vision read is stored as PARSED VALUES ONLY (teams, kickoffs, odds,
-- bookmaker label), never the image. Rule changes are proposed, confirmed with YES and logged.
-- Requires the P0 capture migration (20261126000000).
--
-- Privileges: the Edge Function acts as service_role and may select and insert (and bump one daily
-- counter). The restricted runtime role (used by the Python workflow) may only read the tickets,
-- rows and rules it needs. Nothing is updated or deleted anywhere else.
--
-- Idempotent: applying this file a second time changes nothing.

-- Telegram update ids already handled: a retried delivery is recognised and ignored.
create table if not exists proracle.intake_updates (
    update_id bigint primary key,
    received_at timestamptz not null default now(),
    kind text not null check (kind in ('IMAGE', 'PDF', 'COMMAND', 'TEXT', 'REJECTED'))
);

comment on table proracle.intake_updates is
    'Append-only ids of handled Telegram updates (retry deduplication). No message content.';

-- Vision calls used per UTC day: the spend cap. The only table that is ever updated.
create table if not exists proracle.intake_usage (
    usage_date date primary key,
    vision_calls integer not null default 0 check (vision_calls >= 0),
    updated_at timestamptz not null default now()
);

comment on table proracle.intake_usage is
    'Vision calls per UTC day. Changed only through intake_reserve_call.';

create or replace function proracle.intake_reserve_call(p_date date, p_cap integer)
returns boolean
language plpgsql
set search_path = ''
as $reserve$
declare
    used integer;
begin
    if p_cap is null or p_cap < 1 then
        return false;
    end if;
    insert into proracle.intake_usage as u (usage_date, vision_calls)
    values (p_date, 1)
    on conflict (usage_date) do update
        set vision_calls = u.vision_calls + 1, updated_at = now()
        where u.vision_calls < p_cap
    returning u.vision_calls into used;
    return used is not null;
end
$reserve$;

comment on function proracle.intake_reserve_call(date, integer) is
    'Atomically takes one vision call for the day; false when the cap is already reached.';

create table if not exists proracle.intake_tickets (
    id uuid primary key default gen_random_uuid(),
    update_id bigint not null unique references proracle.intake_updates(update_id) on delete restrict,
    received_at timestamptz not null default now(),
    ticket_date date not null,
    source_kind text not null check (source_kind in ('IMAGE', 'PDF')),
    bookmaker_label text check (bookmaker_label is null or length(btrim(bookmaker_label)) between 1 and 40),
    vision_model text not null check (length(btrim(vision_model)) > 0),
    rows_read smallint not null check (rows_read between 0 and 500),
    rows_kept smallint not null check (rows_kept between 0 and 20),
    rows_matched smallint not null check (rows_matched between 0 and 20),
    check (rows_kept <= rows_read and rows_matched <= rows_kept)
);

comment on table proracle.intake_tickets is
    'Append-only record of one screenshot or PDF read. Parsed values only; the file is never stored.';

create table if not exists proracle.intake_ticket_rows (
    id uuid primary key default gen_random_uuid(),
    intake_id uuid not null references proracle.intake_tickets(id) on delete restrict,
    position smallint not null check (position between 1 and 20),
    home_team text not null check (length(btrim(home_team)) between 1 and 60),
    away_team text not null check (length(btrim(away_team)) between 1 and 60),
    kickoff_at timestamptz,
    home_odds numeric(8, 3) check (home_odds is null or home_odds between 1.001 and 1000),
    draw_odds numeric(8, 3) check (draw_odds is null or draw_odds between 1.001 and 1000),
    away_odds numeric(8, 3) check (away_odds is null or away_odds between 1.001 and 1000),
    matched_fixture_id uuid references proracle.fixtures(id) on delete restrict,
    observed_at timestamptz not null default now(),
    unique (intake_id, position),
    check ((home_odds is null) = (draw_odds is null) and (draw_odds is null) = (away_odds is null))
);

comment on table proracle.intake_ticket_rows is
    'Append-only parsed fixtures of a screenshot: teams, kickoff, three decimal odds, and the stored fixture matched, if exactly one.';

create table if not exists proracle.intake_rules (
    id uuid primary key default gen_random_uuid(),
    version integer not null unique check (version >= 1),
    structure jsonb not null check (jsonb_typeof(structure) = 'object'),
    source_text text not null check (length(btrim(source_text)) between 1 and 300),
    proposed_update_id bigint references proracle.intake_updates(update_id) on delete restrict,
    created_at timestamptz not null default now()
);

comment on table proracle.intake_rules is
    'Append-only versioned rule sets in a fixed JSON structure. A rule is only ever used after a YES (see intake_rule_events).';

create table if not exists proracle.intake_rule_events (
    id uuid primary key default gen_random_uuid(),
    rule_id uuid not null references proracle.intake_rules(id) on delete restrict,
    event text not null check (event in ('PROPOSED', 'ACTIVATED', 'DECLINED', 'RETIRED')),
    update_id bigint references proracle.intake_updates(update_id) on delete restrict,
    created_at timestamptz not null default clock_timestamp()
);

comment on table proracle.intake_rule_events is
    'Append-only rule lifecycle log. The active rule set is the one whose latest ACTIVATED or RETIRED event is ACTIVATED.';

create index if not exists intake_rule_events_rule_idx on proracle.intake_rule_events (rule_id, created_at desc);

-- Free-text-free audit log of what the intake did, including refusals. No message content.
create table if not exists proracle.intake_audit (
    id bigint generated always as identity primary key,
    created_at timestamptz not null default now(),
    update_id bigint,
    action text not null check (action in (
        'TICKET', 'RULE_PROPOSED', 'RULE_ACTIVATED', 'RULE_DECLINED', 'VIEW', 'HELP',
        'REJECTED_CHAT', 'CAP_REACHED', 'BAD_INPUT', 'DISPATCH', 'ERROR'
    )),
    outcome text not null check (outcome in ('OK', 'REFUSED', 'ERROR')),
    detail text check (detail is null or length(detail) <= 200)
);

comment on table proracle.intake_audit is
    'Append-only audit of intake actions and refusals; short fixed details only, never message or image content.';

-- Screenshot tickets reuse the ticket tables so every arm is scored the same way.
alter table proracle.tickets drop constraint if exists tickets_ticket_kind_check;
alter table proracle.tickets
    add constraint tickets_ticket_kind_check check (ticket_kind in ('SIMULATED', 'SCREENSHOT'));

alter table proracle.tickets add column if not exists intake_id uuid
    references proracle.intake_tickets(id) on delete restrict;

alter table proracle.tickets drop constraint if exists tickets_kind_intake_check;
alter table proracle.tickets
    add constraint tickets_kind_intake_check check ((ticket_kind = 'SCREENSHOT') = (intake_id is not null));

-- One simulated ticket per day and design version stays enforced; several screenshot tickets may
-- share a date, but each intake read produces at most one ticket.
alter table proracle.tickets drop constraint if exists tickets_ticket_date_ticket_kind_design_version_key;
create unique index if not exists tickets_one_simulated_per_day_idx
    on proracle.tickets (ticket_date, design_version) where ticket_kind = 'SIMULATED';
create unique index if not exists tickets_one_per_intake_idx
    on proracle.tickets (intake_id) where intake_id is not null;

-- Arm B's adjustment, recorded explicitly (arm C keeps using `forced`).
alter table proracle.ticket_arm_picks add column if not exists adjustment text
    check (adjustment is null or adjustment in ('FORCED_DRAW', 'REVERSED'));
alter table proracle.ticket_arm_picks drop constraint if exists ticket_arm_picks_adjustment_arm_check;
alter table proracle.ticket_arm_picks
    add constraint ticket_arm_picks_adjustment_arm_check check (arm = 'B' or adjustment is null);

-- The rule set in force: the newest ACTIVATED rule with no later RETIRED event. At most one row.
create or replace view proracle.intake_active_rule with (security_invoker = true) as
select r.id as rule_id, r.version, r.structure, r.source_text, last.created_at as activated_at
from proracle.intake_rules r
join lateral (
    select ev.event, ev.created_at
      from proracle.intake_rule_events ev
     where ev.rule_id = r.id and ev.event in ('ACTIVATED', 'RETIRED')
     order by ev.created_at desc, ev.id desc
     limit 1
) last on last.event = 'ACTIVATED'
order by last.created_at desc
limit 1;

-- The proposal waiting for YES or NO: the newest rule whose latest event is PROPOSED, for one hour.
create or replace view proracle.intake_pending_rule with (security_invoker = true) as
select r.id as rule_id, r.version, r.structure, r.source_text, last.created_at as proposed_at
from proracle.intake_rules r
join lateral (
    select ev.event, ev.created_at
      from proracle.intake_rule_events ev
     where ev.rule_id = r.id
     order by ev.created_at desc, ev.id desc
     limit 1
) last on last.event = 'PROPOSED' and last.created_at > now() - interval '1 hour'
order by last.created_at desc
limit 1;

-- Hit rate of each arm on settled picks of COMPLETE tickets (every fixture stored), by ticket kind.
-- A calibration report card, not evidence of an edge.
create or replace view proracle.intake_arm_scoreboard with (security_invoker = true) as
with current_results as (
    select r.fixture_id, r.actual_outcome
      from proracle.results r
     where r.status = 'FINAL'
       and not exists (select 1 from proracle.results s where s.supersedes_result_id = r.id)
)
select t.ticket_kind, p.arm,
       count(distinct p.ticket_id) as tickets,
       count(*) as picks_settled,
       count(*) filter (where p.pick = c.actual_outcome) as picks_right
  from proracle.ticket_arm_picks p
  join proracle.tickets t on t.id = p.ticket_id
  join current_results c on c.fixture_id = p.fixture_id
 where (select count(*) from proracle.ticket_fixtures f where f.ticket_id = t.id) = t.fixture_count
 group by t.ticket_kind, p.arm;

-- Privileges: revoke everything on the new objects, then grant exactly what each role needs.
revoke all privileges on table
    proracle.intake_updates, proracle.intake_usage, proracle.intake_tickets,
    proracle.intake_ticket_rows, proracle.intake_rules, proracle.intake_rule_events,
    proracle.intake_audit, proracle.intake_active_rule, proracle.intake_pending_rule,
    proracle.intake_arm_scoreboard
    from public, anon, authenticated, service_role, proracle_runtime;
revoke all on function proracle.intake_reserve_call(date, integer)
    from public, anon, authenticated, service_role, proracle_runtime;

grant select, insert on table
    proracle.intake_updates, proracle.intake_tickets, proracle.intake_ticket_rows,
    proracle.intake_rules, proracle.intake_rule_events, proracle.intake_audit
    to service_role;
grant select, insert on table proracle.intake_usage to service_role;
grant update (vision_calls, updated_at) on table proracle.intake_usage to service_role;
grant select on table
    proracle.intake_active_rule, proracle.intake_pending_rule, proracle.intake_arm_scoreboard
    to service_role;
grant execute on function proracle.intake_reserve_call(date, integer) to service_role;

-- The Python workflow reads the ticket it computes arms for, and the rule set in force.
grant select on table
    proracle.intake_tickets, proracle.intake_ticket_rows, proracle.intake_rules,
    proracle.intake_rule_events
    to proracle_runtime;
grant select on table proracle.intake_active_rule to proracle_runtime;

alter table proracle.intake_updates enable row level security;
alter table proracle.intake_usage enable row level security;
alter table proracle.intake_tickets enable row level security;
alter table proracle.intake_ticket_rows enable row level security;
alter table proracle.intake_rules enable row level security;
alter table proracle.intake_rule_events enable row level security;
alter table proracle.intake_audit enable row level security;

-- service_role bypasses RLS; the runtime role needs a select policy on what it reads. Created only
-- when missing so a second application changes nothing.
do $policies$
declare
    table_name text;
begin
    foreach table_name in array array[
        'intake_tickets', 'intake_ticket_rows', 'intake_rules', 'intake_rule_events'
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
    end loop;
end
$policies$;
