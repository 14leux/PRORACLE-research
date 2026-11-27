-- PRORACLE tickets and slates (see docs/INTAKE.md and docs/TICKETS.md): one combined migration for the
-- Telegram intake's next stage. A stored read is a TICKET (a declared slip, at most 20 rows, frozen arms)
-- or a SLATE (any size up to 100 stored rows, matched and kept for testing models, no frozen picks).
-- Requires the multi-rule migration (20261128000000).
--
-- Additive and safe to apply before the code that uses it. Nothing the deployed function or the current
-- workflows read is removed or changed: every new column is nullable or defaulted, the older views stay,
-- and the only existing rules relaxed are the 20-row limits, which now depend on the read's kind so a
-- TICKET stays capped at 20. Nothing is updated or deleted. Idempotent: applying this file a second
-- time changes nothing. A rollback script (supabase/tests/rollback_ticket_slate.sql) removes only what
-- this file adds.
--
-- Stored are parsed values, reasons, counts, Telegram file ids (for 60 minutes) and a SHA-256 of a file's
-- bytes. Never a file, never message text.

-- 1. A stored read is a ticket or a slate; its token use is kept per read (the vision call's input and
--    output tokens; null for a read that made no vision call).
alter table proracle.intake_tickets add column if not exists kind text not null default 'TICKET';
alter table proracle.intake_tickets add column if not exists input_tokens integer;
alter table proracle.intake_tickets add column if not exists output_tokens integer;

alter table proracle.intake_tickets drop constraint if exists intake_tickets_kind_check;
alter table proracle.intake_tickets
    add constraint intake_tickets_kind_check check (kind in ('TICKET', 'SLATE'));
alter table proracle.intake_tickets drop constraint if exists intake_tickets_tokens_check;
alter table proracle.intake_tickets
    add constraint intake_tickets_tokens_check check (
        (input_tokens is null or input_tokens >= 0) and (output_tokens is null or output_tokens >= 0)
    );

-- Row limits: a slate keeps up to 100 rows, a ticket stays at 20. rows_read keeps its own limit (500).
alter table proracle.intake_tickets drop constraint if exists intake_tickets_rows_kept_check;
alter table proracle.intake_tickets
    add constraint intake_tickets_rows_kept_check check (rows_kept between 0 and 100);
alter table proracle.intake_tickets drop constraint if exists intake_tickets_rows_matched_check;
alter table proracle.intake_tickets
    add constraint intake_tickets_rows_matched_check check (rows_matched between 0 and 100);
alter table proracle.intake_tickets drop constraint if exists intake_tickets_ticket_limits_check;
alter table proracle.intake_tickets
    add constraint intake_tickets_ticket_limits_check check (
        kind = 'SLATE' or (rows_kept <= 20 and rows_matched <= 20)
    );

-- The kind is part of a row's parent key so the 20-row limit on a ticket's rows can be a plain check.
do $key$
begin
    if not exists (
        select 1 from pg_constraint
         where conrelid = 'proracle.intake_tickets'::regclass and conname = 'intake_tickets_id_kind_key'
    ) then
        alter table proracle.intake_tickets add constraint intake_tickets_id_kind_key unique (id, kind);
    end if;
end
$key$;

-- 2. Rows: how each row matched (or why not), and the larger position range for a slate. read_kind is
--    the parent's kind, copied by the writer and enforced by a foreign key.
alter table proracle.intake_ticket_rows add column if not exists read_kind text not null default 'TICKET';
alter table proracle.intake_ticket_rows add column if not exists match_via text;
alter table proracle.intake_ticket_rows add column if not exists unmatched_reason text;

alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_read_kind_check;
alter table proracle.intake_ticket_rows
    add constraint intake_ticket_rows_read_kind_check check (read_kind in ('TICKET', 'SLATE'));
alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_match_via_check;
alter table proracle.intake_ticket_rows
    add constraint intake_ticket_rows_match_via_check
    check (match_via is null or match_via in ('EXACT', 'ALIAS', 'LOOSE', 'ANCHOR'));
alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_unmatched_reason_check;
alter table proracle.intake_ticket_rows
    add constraint intake_ticket_rows_unmatched_reason_check
    check (unmatched_reason is null or unmatched_reason in ('NO_KICKOFF', 'NO_MATCH', 'AMBIGUOUS'));
alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_match_reason_check;
alter table proracle.intake_ticket_rows
    add constraint intake_ticket_rows_match_reason_check check (
        (match_via is null or matched_fixture_id is not null)
        and (unmatched_reason is null or matched_fixture_id is null)
    );

alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_position_check;
alter table proracle.intake_ticket_rows
    add constraint intake_ticket_rows_position_check check (position between 1 and 100);
alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_ticket_position_check;
alter table proracle.intake_ticket_rows
    add constraint intake_ticket_rows_ticket_position_check check (read_kind = 'SLATE' or position <= 20);

do $rowkey$
begin
    if not exists (
        select 1 from pg_constraint
         where conrelid = 'proracle.intake_ticket_rows'::regclass
           and conname = 'intake_ticket_rows_intake_kind_fkey'
    ) then
        alter table proracle.intake_ticket_rows
            add constraint intake_ticket_rows_intake_kind_fkey
            foreign key (intake_id, read_kind) references proracle.intake_tickets (id, kind)
            on delete restrict;
    end if;
end
$rowkey$;

-- 3. Rows that were read but not used, with a fixed reason (no free text).
create table if not exists proracle.intake_rejected_rows (
    id uuid primary key default gen_random_uuid(),
    intake_id uuid not null references proracle.intake_tickets(id) on delete restrict,
    position smallint not null check (position between 1 and 500),
    home_team text check (home_team is null or length(btrim(home_team)) between 1 and 60),
    away_team text check (away_team is null or length(btrim(away_team)) between 1 and 60),
    reason text not null check (reason in (
        'NOT_A_FIXTURE', 'MISSING_TEAM', 'SAME_TEAM', 'OVER_LIMIT'
    )),
    observed_at timestamptz not null default now(),
    unique (intake_id, position)
);

comment on table proracle.intake_rejected_rows is
    'Append-only rows a read produced but the intake did not use, each with a fixed reason code. Names only; never the file.';

-- 4. The upload waiting for the answer to "ticket or slate?": the Telegram file id only, for 60 minutes.
--    A read made from it points back here (intake_tickets.pending_upload_id), which is how it is used up.
create table if not exists proracle.intake_pending_uploads (
    id uuid primary key default gen_random_uuid(),
    update_id bigint not null unique references proracle.intake_updates(update_id) on delete restrict,
    file_id text not null check (length(btrim(file_id)) between 1 and 300),
    file_type text not null check (file_type in ('IMAGE', 'PDF')),
    file_size bigint check (file_size is null or file_size between 1 and 20971520),
    created_at timestamptz not null default clock_timestamp(),
    expires_at timestamptz not null default (clock_timestamp() + interval '60 minutes'),
    check (expires_at > created_at and expires_at <= created_at + interval '2 hours')
);

comment on table proracle.intake_pending_uploads is
    'Append-only Telegram file ids awaiting the ticket-or-slate answer. Expires after 60 minutes; the newest one is the only one offered (intake_pending_upload). No file content.';

alter table proracle.intake_tickets add column if not exists pending_upload_id uuid
    references proracle.intake_pending_uploads(id) on delete restrict;
create unique index if not exists intake_tickets_one_per_pending_idx
    on proracle.intake_tickets (pending_upload_id) where pending_upload_id is not null;

-- The one upload that can still be answered: the newest, not expired, not already read.
create or replace view proracle.intake_pending_upload with (security_invoker = true) as
select p.id, p.update_id, p.file_id, p.file_type, p.file_size, p.created_at, p.expires_at
from (
    select u.* from proracle.intake_pending_uploads u order by u.created_at desc, u.id desc limit 1
) p
where p.expires_at > now()
  and not exists (select 1 from proracle.intake_tickets t where t.pending_upload_id = p.id);

-- 5. A hash of a file's bytes (never the file), so an identical repeat upload can reuse the earlier read.
create table if not exists proracle.intake_file_hashes (
    id uuid primary key default gen_random_uuid(),
    file_sha256 text not null check (file_sha256 ~ '^[0-9a-f]{64}$'),
    intake_id uuid not null references proracle.intake_tickets(id) on delete restrict,
    created_at timestamptz not null default clock_timestamp(),
    unique (file_sha256, intake_id)
);

comment on table proracle.intake_file_hashes is
    'Append-only SHA-256 of an uploaded file mapped to the read made from it, so a repeat upload can reuse that read. A hash only; never the file.';

create index if not exists intake_file_hashes_sha_idx on proracle.intake_file_hashes (file_sha256, created_at desc);

-- 6. Daily token totals next to the daily call cap, for the token budget. Existing counters and the
--    existing reserve function are untouched; a new function adds one read's tokens.
alter table proracle.intake_usage add column if not exists input_tokens bigint not null default 0;
alter table proracle.intake_usage add column if not exists output_tokens bigint not null default 0;
alter table proracle.intake_usage drop constraint if exists intake_usage_tokens_check;
alter table proracle.intake_usage
    add constraint intake_usage_tokens_check check (input_tokens >= 0 and output_tokens >= 0);

create or replace function proracle.intake_add_tokens(p_date date, p_input bigint, p_output bigint)
returns void
language plpgsql
set search_path = ''
as $tokens$
begin
    if p_input is null or p_output is null or p_input < 0 or p_output < 0 then
        raise exception 'token counts must be zero or more';
    end if;
    insert into proracle.intake_usage as u (usage_date, vision_calls, input_tokens, output_tokens)
    values (p_date, 0, p_input, p_output)
    on conflict (usage_date) do update
        set input_tokens = u.input_tokens + p_input,
            output_tokens = u.output_tokens + p_output,
            updated_at = now();
end
$tokens$;

comment on function proracle.intake_add_tokens(date, bigint, bigint) is
    'Adds one vision read''s input and output tokens to the UTC day''s totals (the daily token budget).';

-- 7. A ticket stores when its last fixture kicks off and when its review falls due (UTC). Null on older
--    tickets; the review workflow falls back to the ticket's latest pick kickoff when null.
alter table proracle.tickets add column if not exists last_kickoff_at timestamptz;
alter table proracle.tickets add column if not exists review_due_at timestamptz;
alter table proracle.tickets drop constraint if exists tickets_review_due_check;
alter table proracle.tickets
    add constraint tickets_review_due_check check (
        (last_kickoff_at is null) = (review_due_at is null)
        and (review_due_at is null or review_due_at > last_kickoff_at)
    );

-- 8. The automatic post-fixtures review: one row per ticket, written after the report is sent, so a
--    rerun finds it and sends nothing (a second row for a ticket is refused).
create table if not exists proracle.ticket_reviews (
    id uuid primary key default gen_random_uuid(),
    ticket_id uuid not null unique references proracle.tickets(id) on delete restrict,
    reviewed_at timestamptz not null default now(),
    completeness text not null check (completeness in ('COMPLETE', 'PARTIAL')),
    picks_total smallint not null check (picks_total >= 0),
    picks_settled smallint not null check (picks_settled between 0 and picks_total),
    summary jsonb not null check (jsonb_typeof(summary) = 'object'),
    telegram_message_id bigint
);

comment on table proracle.ticket_reviews is
    'Append-only post-fixtures reviews, one per ticket (unique ticket_id makes each review send once). Counts and a small summary only.';

-- 9. Recent settled picks per arm, for the plain-language performance message: the last 28 days against
--    the 28 days before, on settled picks of COMPLETE tickets, with arm A on the same fixtures.
create or replace view proracle.intake_recent_window with (security_invoker = true) as
with current_results as (
    select r.fixture_id, r.actual_outcome
      from proracle.results r
     where r.status = 'FINAL'
       and not exists (select 1 from proracle.results s where s.supersedes_result_id = r.id)
),
complete_tickets as (
    select t.id, t.ticket_kind
      from proracle.tickets t
     where (select count(*) from proracle.ticket_fixtures f where f.ticket_id = t.id) = t.fixture_count
),
settled as (
    select t.ticket_kind, p.ticket_id, p.fixture_id, p.arm, p.arm_version, p.pick, c.actual_outcome,
           case when p.kickoff_at >= now() - interval '28 days' then 'RECENT'
                when p.kickoff_at >= now() - interval '56 days' then 'PRIOR'
           end as window_name
      from proracle.ticket_arm_picks p
      join complete_tickets t on t.id = p.ticket_id
      join current_results c on c.fixture_id = p.fixture_id
     where p.kickoff_at >= now() - interval '56 days'
)
select s.ticket_kind, s.arm, s.arm_version, s.window_name,
       count(*) as picks_settled,
       count(*) filter (where s.pick = s.actual_outcome) as picks_right,
       count(*) filter (where a.pick = s.actual_outcome) as a_right_same_fixtures
  from settled s
  left join settled a on a.ticket_id = s.ticket_id and a.fixture_id = s.fixture_id and a.arm = 'A'
 group by s.ticket_kind, s.arm, s.arm_version, s.window_name;

-- 10. More things the intake records in its audit log (fixed words only, as before).
alter table proracle.intake_audit drop constraint if exists intake_audit_action_check;
alter table proracle.intake_audit
    add constraint intake_audit_action_check check (action in (
        'TICKET', 'RULE_PROPOSED', 'RULE_ACTIVATED', 'RULE_DECLINED', 'RULE_RETIRE_REQUESTED',
        'RULE_RETIRED', 'VIEW', 'HELP', 'REJECTED_CHAT', 'CAP_REACHED', 'BAD_INPUT', 'DISPATCH', 'ERROR',
        'SLATE', 'PENDING', 'REUSED', 'TOKEN_CAP', 'REVIEW', 'ELI5'
    ));

-- 11. Privileges: nothing by default on the new objects, then exactly what each role needs. Existing
--     grants on the existing tables stay as they are (the new columns follow their table's grants).
revoke all privileges on table
    proracle.intake_rejected_rows, proracle.intake_pending_uploads, proracle.intake_file_hashes,
    proracle.ticket_reviews, proracle.intake_pending_upload, proracle.intake_recent_window
    from public, anon, authenticated, service_role, proracle_runtime;
revoke all on function proracle.intake_add_tokens(date, bigint, bigint)
    from public, anon, authenticated, service_role, proracle_runtime;

grant select, insert on table
    proracle.intake_rejected_rows, proracle.intake_pending_uploads, proracle.intake_file_hashes
    to service_role;
grant select on table proracle.ticket_reviews to service_role;
grant select on table proracle.intake_pending_upload, proracle.intake_recent_window to service_role;
grant update (input_tokens, output_tokens) on table proracle.intake_usage to service_role;
grant execute on function proracle.intake_add_tokens(date, bigint, bigint) to service_role;

-- The Python workflow writes each ticket's review and reads the rejected rows it may report on.
grant select, insert on table proracle.ticket_reviews to proracle_runtime;
grant select on table proracle.intake_rejected_rows to proracle_runtime;

alter table proracle.intake_rejected_rows enable row level security;
alter table proracle.intake_pending_uploads enable row level security;
alter table proracle.intake_file_hashes enable row level security;
alter table proracle.ticket_reviews enable row level security;

do $policies$
declare
    rule record;
begin
    for rule in
        select * from (values
            ('intake_rejected_rows', 'select'),
            ('ticket_reviews', 'select'),
            ('ticket_reviews', 'insert')
        ) as v(table_name, command)
    loop
        if not exists (
            select 1 from pg_policies
             where schemaname = 'proracle' and tablename = rule.table_name
               and policyname = 'proracle_runtime_' || rule.command || '_' || rule.table_name
        ) then
            if rule.command = 'select' then
                execute format(
                    'create policy %I on proracle.%I for select to proracle_runtime using (true)',
                    'proracle_runtime_select_' || rule.table_name, rule.table_name
                );
            else
                execute format(
                    'create policy %I on proracle.%I for insert to proracle_runtime with check (true)',
                    'proracle_runtime_insert_' || rule.table_name, rule.table_name
                );
            end if;
        end if;
    end loop;
end
$policies$;
