-- PRORACLE: a ticket counts as complete only when every arm's picks were fully written (IMP-065).
--
-- The scoreboard and the recent window counted a ticket as complete once all its fixtures were stored. A
-- ticket is written in three requests (ticket, fixtures, picks), so a stopped append could leave arm picks
-- short and the ticket still counted. Now every arm present must hold one pick per fixture, and arms A and C
-- must both be present. Same columns, same grants, same readers: only the definition of "complete" changes.
-- On the data at the time of writing no ticket is excluded by it.
--
-- Additive and repeatable (create or replace of two views). Rollback: supabase/tests/rollback_complete_tickets.sql

create or replace view proracle.intake_scoreboard with (security_invoker = true) as
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
       -- every arm present holds exactly one pick per fixture, and arms A and C are both there: a stopped
       -- append that left some arm short must not count (IMP-065)
       and not exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id
                        group by p.arm, p.arm_version having count(*) <> t.fixture_count)
       and exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id and p.arm = 'A')
       and exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id and p.arm = 'C')
),
settled as (
    select t.ticket_kind, p.ticket_id, p.fixture_id, p.arm, p.arm_version, p.pick,
           (p.adjustment is not null or p.forced) as adjusted, c.actual_outcome
      from proracle.ticket_arm_picks p
      join complete_tickets t on t.id = p.ticket_id
      join current_results c on c.fixture_id = p.fixture_id
)
select s.ticket_kind, s.arm, s.arm_version,
       count(distinct s.ticket_id) as tickets,
       count(*) as picks_settled,
       count(*) filter (where s.pick = s.actual_outcome) as picks_right,
       count(*) filter (where a.pick = s.actual_outcome) as a_right_same_fixtures,
       count(*) filter (where c.pick = s.actual_outcome) as c_right_same_fixtures,
       count(*) filter (where s.adjusted) as adjusted_settled,
       count(*) filter (where s.adjusted and s.pick = s.actual_outcome) as adjusted_right,
       count(*) filter (where s.adjusted and a.pick = s.actual_outcome) as a_right_on_adjusted
  from settled s
  left join settled a on a.ticket_id = s.ticket_id and a.fixture_id = s.fixture_id and a.arm = 'A'
  left join settled c on c.ticket_id = s.ticket_id and c.fixture_id = s.fixture_id and c.arm = 'C'
 group by s.ticket_kind, s.arm, s.arm_version;

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
       -- every arm present holds exactly one pick per fixture, and arms A and C are both there: a stopped
       -- append that left some arm short must not count (IMP-065)
       and not exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id
                        group by p.arm, p.arm_version having count(*) <> t.fixture_count)
       and exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id and p.arm = 'A')
       and exists (select 1 from proracle.ticket_arm_picks p where p.ticket_id = t.id and p.arm = 'C')
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
