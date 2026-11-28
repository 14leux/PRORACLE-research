-- PRORACLE: two read-only views for the plain-language performance message (/eli5): one per window
-- (the last 7 days, the last 28 days, and the 28 days before those) and one per ticket and arm.
--
-- Additive and repeatable: two views and their grant, nothing else. They remove and change nothing the
-- deployed function or the workflows read (intake_recent_window and intake_scoreboard stay as they are).
-- Both run as the caller (security_invoker) and only service_role (the intake function) can read them.
-- Counts only: no names, no prices, no stakes. A ticket counts only when it is complete in the same sense
-- as the scoreboard (every fixture stored, every arm present whole, arms A and C both there).
--
-- Rollback: supabase/tests/rollback_window_ticket_views.sql

-- 1. The picks of complete tickets, settled, per arm, per window. The windows overlap on purpose (the last
--    28 days contain the last 7), so a pick can be in two rows. arm A's right-count on exactly the same
--    fixtures sits beside every arm's, which is what "ahead of or behind A" is measured on.
create or replace view proracle.intake_window_summary with (security_invoker = true) as
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
       and not exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id
                        group by p.arm, p.arm_version having count(*) <> t.fixture_count)
       and exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id and p.arm = 'A')
       and exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id and p.arm = 'C')
),
settled as (
    select t.ticket_kind, p.ticket_id, p.fixture_id, p.arm, p.arm_version, p.pick, p.kickoff_at, c.actual_outcome
      from proracle.ticket_arm_picks p
      join complete_tickets t on t.id = p.ticket_id
      join current_results c on c.fixture_id = p.fixture_id
     where p.kickoff_at >= now() - interval '56 days'
),
windows(window_name, from_at, until_at) as (
    values ('LAST7', now() - interval '7 days', 'infinity'::timestamptz),
           ('LAST28', now() - interval '28 days', 'infinity'::timestamptz),
           ('PRIOR28', now() - interval '56 days', now() - interval '28 days')
)
select s.ticket_kind, s.arm, s.arm_version, w.window_name,
       count(*) as picks_settled,
       count(*) filter (where s.pick = s.actual_outcome) as picks_right,
       count(*) filter (where a.pick = s.actual_outcome) as a_right_same_fixtures
  from settled s
  join windows w on s.kickoff_at >= w.from_at and s.kickoff_at < w.until_at
  left join settled a on a.ticket_id = s.ticket_id and a.fixture_id = s.fixture_id and a.arm = 'A'
 group by s.ticket_kind, s.arm, s.arm_version, w.window_name;

comment on view proracle.intake_window_summary is
    'Counts only: settled picks per ticket kind, arm and window (LAST7, LAST28, PRIOR28) on complete tickets, with arm A right-count on the same fixtures. Read by the intake function for /eli5.';

-- 2. One row per complete ticket and arm that has settled picks: how many settled and how many were right,
--    and the latest kickoff among them (so the newest tickets come first and old ones can be ignored).
create or replace view proracle.intake_ticket_hits with (security_invoker = true) as
with current_results as (
    select r.fixture_id, r.actual_outcome
      from proracle.results r
     where r.status = 'FINAL'
       and not exists (select 1 from proracle.results s where s.supersedes_result_id = r.id)
),
complete_tickets as (
    select t.id, t.ticket_kind, t.ticket_date, t.fixture_count
      from proracle.tickets t
     where (select count(*) from proracle.ticket_fixtures f where f.ticket_id = t.id) = t.fixture_count
       and not exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id
                        group by p.arm, p.arm_version having count(*) <> t.fixture_count)
       and exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id and p.arm = 'A')
       and exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id and p.arm = 'C')
)
select t.id as ticket_id, t.ticket_kind, t.ticket_date, p.arm, p.arm_version, t.fixture_count,
       count(*) as picks_settled,
       count(*) filter (where p.pick = c.actual_outcome) as picks_right,
       max(p.kickoff_at) as last_kickoff_at
  from proracle.ticket_arm_picks p
  join complete_tickets t on t.id = p.ticket_id
  join current_results c on c.fixture_id = p.fixture_id
 where p.kickoff_at >= now() - interval '56 days'
 group by t.id, t.ticket_kind, t.ticket_date, p.arm, p.arm_version, t.fixture_count;

comment on view proracle.intake_ticket_hits is
    'Counts only: settled and right picks per complete ticket and arm, with the latest kickoff among them. Read by the intake function for /eli5 ticket spikes.';

revoke all privileges on table proracle.intake_window_summary, proracle.intake_ticket_hits
    from public, anon, authenticated, service_role, proracle_runtime;
grant select on table proracle.intake_window_summary, proracle.intake_ticket_hits to service_role;
