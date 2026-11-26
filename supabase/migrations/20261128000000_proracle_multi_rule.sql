-- PRORACLE multi-rule arms (see docs/INTAKE.md): up to five rule sets run at once, each as its own
-- arm B pick set on the same fixtures, beside arm A and arm C. Requires the intake migration
-- (20261127000000).
--
-- Additive and safe to apply before the code that uses it: the older view `intake_active_rule` and
-- `intake_arm_scoreboard` stay as they are, the older function and workflow keep working, and nothing
-- is updated or deleted. Idempotent: applying this file a second time changes nothing.

-- 1. Several arm B picks per ticket fixture, told apart by arm_version ("rule-v<N>"). Arms A and C
--    stay one pick per ticket fixture, as before.
alter table proracle.ticket_arm_picks drop constraint if exists ticket_arm_picks_ticket_id_fixture_id_arm_key;
alter table proracle.ticket_arm_picks drop constraint if exists ticket_arm_picks_ticket_fixture_arm_version_key;
alter table proracle.ticket_arm_picks
    add constraint ticket_arm_picks_ticket_fixture_arm_version_key
    unique (ticket_id, fixture_id, arm, arm_version);
create unique index if not exists ticket_arm_picks_one_a_c_per_fixture_idx
    on proracle.ticket_arm_picks (ticket_id, fixture_id, arm) where arm <> 'B';

-- 2. Retiring a rule is confirmed with YES like any other rule change, so it has its own events.
alter table proracle.intake_rule_events drop constraint if exists intake_rule_events_event_check;
alter table proracle.intake_rule_events
    add constraint intake_rule_events_event_check check (event in (
        'PROPOSED', 'ACTIVATED', 'DECLINED', 'RETIRED', 'RETIRE_REQUESTED', 'RETIRE_DECLINED'
    ));

alter table proracle.intake_audit drop constraint if exists intake_audit_action_check;
alter table proracle.intake_audit
    add constraint intake_audit_action_check check (action in (
        'TICKET', 'RULE_PROPOSED', 'RULE_ACTIVATED', 'RULE_DECLINED', 'RULE_RETIRE_REQUESTED',
        'RULE_RETIRED', 'VIEW', 'HELP', 'REJECTED_CHAT', 'CAP_REACHED', 'BAD_INPUT', 'DISPATCH', 'ERROR'
    ));

-- 3. Every rule set in force: each rule whose latest ACTIVATED or RETIRED event is ACTIVATED.
--    The function allows at most five; the Python workflow refuses to run if more than five appear.
create or replace view proracle.intake_active_rules with (security_invoker = true) as
select r.id as rule_id, r.version, r.structure, r.source_text, last.created_at as activated_at
from proracle.intake_rules r
join lateral (
    select ev.event, ev.created_at
      from proracle.intake_rule_events ev
     where ev.rule_id = r.id and ev.event in ('ACTIVATED', 'RETIRED')
     order by ev.created_at desc, ev.id desc
     limit 1
) last on last.event = 'ACTIVATED'
order by r.version;

-- The retirement waiting for YES or NO: the newest rule whose latest event is RETIRE_REQUESTED, for one hour.
create or replace view proracle.intake_pending_retirement with (security_invoker = true) as
select r.id as rule_id, r.version, r.structure, r.source_text, last.created_at as requested_at
from proracle.intake_rules r
join lateral (
    select ev.event, ev.created_at
      from proracle.intake_rule_events ev
     where ev.rule_id = r.id
     order by ev.created_at desc, ev.id desc
     limit 1
) last on last.event = 'RETIRE_REQUESTED' and last.created_at > now() - interval '1 hour'
order by last.created_at desc
limit 1;

-- 4. Hit rate by arm and rule version on settled picks of COMPLETE tickets (every fixture stored),
--    by ticket kind, with what arm A and arm C picked on exactly the same fixtures, and the picks
--    where the arm differs from arm A by design (a forced draw or a reversal), which is the only
--    place a rule can differ from arm A. A calibration report card, not evidence of an edge.
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

-- Privileges: nothing by default, then exactly what each role needs.
revoke all privileges on table
    proracle.intake_active_rules, proracle.intake_pending_retirement, proracle.intake_scoreboard
    from public, anon, authenticated, service_role, proracle_runtime;
grant select on table
    proracle.intake_active_rules, proracle.intake_pending_retirement, proracle.intake_scoreboard
    to service_role;
-- The Python workflow reads every active rule (the underlying tables already have its select policies).
grant select on table proracle.intake_active_rules to proracle_runtime;
