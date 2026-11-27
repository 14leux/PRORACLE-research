-- Rollback for the tickets-and-slates migration (20261129000000): removes exactly what that migration
-- added and puts back the limits and checks it replaced, leaving the earlier schema as it was. Run it
-- with psql against a DATABASE COPY first (see docs/DATABASE.md), never straight against production.
--
-- It refuses to run when that would delete stored data: any row in a new table, any SLATE read, any
-- stored match reason or token count, any ticket with a stored last kickoff, any token total, or any
-- audit row using a new action. To roll back anyway and accept that loss, add  -v allow_data_loss=1.
-- One transaction: either everything is put back or nothing changes.
--
--   psql -X -v ON_ERROR_STOP=1 -f supabase/tests/rollback_ticket_slate.sql
\if :{?allow_data_loss}
select set_config('proracle.allow_data_loss', 'on', false);
\endif

begin;

do $guard$
declare
    found text := '';
begin
    if coalesce(current_setting('proracle.allow_data_loss', true), '') = 'on' then
        return;
    end if;
    if exists (select 1 from proracle.intake_rejected_rows) then found := found || ' rejected rows;'; end if;
    if exists (select 1 from proracle.intake_pending_uploads) then found := found || ' pending uploads;'; end if;
    if exists (select 1 from proracle.intake_file_hashes) then found := found || ' file hashes;'; end if;
    if exists (select 1 from proracle.ticket_reviews) then found := found || ' ticket reviews;'; end if;
    if exists (select 1 from proracle.intake_tickets where kind <> 'TICKET' or input_tokens is not null
                                                          or output_tokens is not null) then
        found := found || ' slate reads or per-read tokens;';
    end if;
    if exists (select 1 from proracle.intake_ticket_rows where match_via is not null
                                                              or unmatched_reason is not null or read_kind <> 'TICKET') then
        found := found || ' stored match reasons;';
    end if;
    if exists (select 1 from proracle.tickets where last_kickoff_at is not null) then
        found := found || ' ticket kickoff and review times;';
    end if;
    if exists (select 1 from proracle.intake_usage where input_tokens > 0 or output_tokens > 0) then
        found := found || ' daily token totals;';
    end if;
    if exists (select 1 from proracle.intake_audit where action in ('SLATE', 'PENDING', 'REUSED', 'TOKEN_CAP', 'REVIEW', 'ELI5')) then
        found := found || ' audit rows with new actions;';
    end if;
    if found <> '' then
        raise exception 'rollback refused, it would delete stored data:% (add -v allow_data_loss=1 to accept that)', found;
    end if;
end
$guard$;

-- views and the function first (they read the objects below)
drop view if exists proracle.intake_recent_window;
drop view if exists proracle.intake_pending_upload;
drop function if exists proracle.intake_add_tokens(date, bigint, bigint);

-- new tables (their policies and grants go with them)
drop table if exists proracle.ticket_reviews;
drop table if exists proracle.intake_file_hashes;
drop table if exists proracle.intake_rejected_rows;

-- intake_tickets: the link to a pending upload, then the pending table itself
drop index if exists proracle.intake_tickets_one_per_pending_idx;
alter table proracle.intake_tickets drop column if exists pending_upload_id;
drop table if exists proracle.intake_pending_uploads;

-- tickets
alter table proracle.tickets drop constraint if exists tickets_review_due_check;
alter table proracle.tickets drop column if exists review_due_at;
alter table proracle.tickets drop column if exists last_kickoff_at;

-- intake_usage
alter table proracle.intake_usage drop constraint if exists intake_usage_tokens_check;
alter table proracle.intake_usage drop column if exists input_tokens;
alter table proracle.intake_usage drop column if exists output_tokens;

-- audit actions: back to the list the multi-rule migration set
alter table proracle.intake_audit drop constraint if exists intake_audit_action_check;
alter table proracle.intake_audit
    add constraint intake_audit_action_check check (action in (
        'TICKET', 'RULE_PROPOSED', 'RULE_ACTIVATED', 'RULE_DECLINED', 'RULE_RETIRE_REQUESTED',
        'RULE_RETIRED', 'VIEW', 'HELP', 'REJECTED_CHAT', 'CAP_REACHED', 'BAD_INPUT', 'DISPATCH', 'ERROR'
    ));

-- intake_ticket_rows: back to positions 1 to 20, without the kind link or the match reasons
alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_intake_kind_fkey;
alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_ticket_position_check;
alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_match_reason_check;
alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_unmatched_reason_check;
alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_match_via_check;
alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_read_kind_check;
alter table proracle.intake_ticket_rows drop constraint if exists intake_ticket_rows_position_check;
alter table proracle.intake_ticket_rows
    add constraint intake_ticket_rows_position_check check (position between 1 and 20);
alter table proracle.intake_ticket_rows drop column if exists unmatched_reason;
alter table proracle.intake_ticket_rows drop column if exists match_via;
alter table proracle.intake_ticket_rows drop column if exists read_kind;

-- intake_tickets: back to 20 rows, without the kind or the token counts
alter table proracle.intake_tickets drop constraint if exists intake_tickets_id_kind_key;
alter table proracle.intake_tickets drop constraint if exists intake_tickets_ticket_limits_check;
alter table proracle.intake_tickets drop constraint if exists intake_tickets_tokens_check;
alter table proracle.intake_tickets drop constraint if exists intake_tickets_kind_check;
alter table proracle.intake_tickets drop constraint if exists intake_tickets_rows_kept_check;
alter table proracle.intake_tickets
    add constraint intake_tickets_rows_kept_check check (rows_kept between 0 and 20);
alter table proracle.intake_tickets drop constraint if exists intake_tickets_rows_matched_check;
alter table proracle.intake_tickets
    add constraint intake_tickets_rows_matched_check check (rows_matched between 0 and 20);
alter table proracle.intake_tickets drop column if exists output_tokens;
alter table proracle.intake_tickets drop column if exists input_tokens;
alter table proracle.intake_tickets drop column if exists kind;

commit;
select 'rollback_ticket_slate: done' as result;
