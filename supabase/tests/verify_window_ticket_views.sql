-- Run via psql against a disposable local Supabase database after `supabase db reset --local`.
-- Verifies the window and ticket views migration (20261202000000): intake_window_summary puts each settled
-- pick of a complete ticket in the right windows (LAST7, LAST28, PRIOR28; the windows overlap), uses the
-- corrected result, leaves out picks older than 56 days, unsettled picks and incomplete tickets, and carries
-- arm A's right-count on the same fixtures; intake_ticket_hits gives one row per complete ticket and arm with
-- settled and right counts and the latest kickoff; privileges are service_role select only. Rolls back
-- everything it writes. Any failed expectation raises an exception and stops psql (use -v ON_ERROR_STOP=1).
begin;

do $verify$
declare
    w1 constant uuid := '00000000-0000-4000-8000-0000000000b1';  -- kicked off 2 days ago
    w2 constant uuid := '00000000-0000-4000-8000-0000000000b2';  -- 10 days ago (result corrected later)
    w3 constant uuid := '00000000-0000-4000-8000-0000000000b3';  -- 40 days ago
    w4 constant uuid := '00000000-0000-4000-8000-0000000000b4';  -- 70 days ago: outside every window
    w5 constant uuid := '00000000-0000-4000-8000-0000000000b5';  -- tomorrow: not settled
    w6 constant uuid := '00000000-0000-4000-8000-0000000000b6';  -- 3 days ago, on an incomplete ticket
    pw1 constant uuid := '00000000-0000-4000-8000-0000000000c1';
    pw2 constant uuid := '00000000-0000-4000-8000-0000000000c2';
    pw3 constant uuid := '00000000-0000-4000-8000-0000000000c3';
    pw4 constant uuid := '00000000-0000-4000-8000-0000000000c4';
    pw5 constant uuid := '00000000-0000-4000-8000-0000000000c5';
    pw6 constant uuid := '00000000-0000-4000-8000-0000000000c6';
    t1 constant uuid := '00000000-0000-4000-8000-0000000000d1';
    t2 constant uuid := '00000000-0000-4000-8000-0000000000d2';
    t3 constant uuid := '00000000-0000-4000-8000-0000000000d3';
    t4 constant uuid := '00000000-0000-4000-8000-0000000000d4';
    t5 constant uuid := '00000000-0000-4000-8000-0000000000d5';
    t6 constant uuid := '00000000-0000-4000-8000-0000000000d6';
    t7 constant uuid := '00000000-0000-4000-8000-0000000000d7';  -- two fixtures, arm C short by one pick
    t8 constant uuid := '00000000-0000-4000-8000-0000000000d8';  -- arm C only, no arm A
    t9 constant uuid := '00000000-0000-4000-8000-0000000000d9';  -- complete, two fixtures at different kickoffs
    w7 constant uuid := '00000000-0000-4000-8000-0000000000b7';  -- kicked off 1 day ago
    w8 constant uuid := '00000000-0000-4000-8000-0000000000b8';  -- kicked off 20 days ago
    pw7 constant uuid := '00000000-0000-4000-8000-0000000000c7';
    pw8 constant uuid := '00000000-0000-4000-8000-0000000000c8';
    res2 uuid;
    n int;
    got record;
    rec record;
    d int := 0;
begin
    -- fixtures, one rating prediction each, results (w2 corrected: first HOME_WIN, then AWAY_WIN)
    insert into proracle.fixtures (id, home_team, away_team, kickoff_at, source_name, source_event_key) values
        (w1, 'P1', 'Q1', now() - interval '2 days', 'synthetic', 'wt-1'),
        (w2, 'P2', 'Q2', now() - interval '10 days', 'synthetic', 'wt-2'),
        (w3, 'P3', 'Q3', now() - interval '40 days', 'synthetic', 'wt-3'),
        (w4, 'P4', 'Q4', now() - interval '70 days', 'synthetic', 'wt-4'),
        (w5, 'P5', 'Q5', now() + interval '1 day', 'synthetic', 'wt-5'),
        (w6, 'P6', 'Q6', now() - interval '3 days', 'synthetic', 'wt-6');
    insert into proracle.predictions (
        id, fixture_id, contract_version, predicted_at, kickoff_at, model_provider, model_name,
        model_version, prompt_version, input_schema_version, input_snapshot_hash,
        home_win_probability, draw_probability, away_win_probability
    )
    select p.pid, p.fid, '1.0', f.kickoff_at - interval '1 day', f.kickoff_at, 'synthetic', 'test-model', '1', 'p', 'features-v1', repeat(p.h, 64), 0.5, 0.3, 0.2
      from (values (pw1, w1, '1'), (pw2, w2, '2'), (pw3, w3, '3'), (pw4, w4, '4'), (pw5, w5, '5'), (pw6, w6, '6')) as p(pid, fid, h)
      join proracle.fixtures f on f.id = p.fid;
    insert into proracle.results (fixture_id, status, actual_outcome, observed_at, finalized_at, source_name) values
        (w1, 'FINAL', 'HOME_WIN', now() - interval '2 days' + interval '3 hours', now() - interval '2 days' + interval '3 hours', 'synthetic'),
        (w3, 'FINAL', 'DRAW', now() - interval '40 days' + interval '3 hours', now() - interval '40 days' + interval '3 hours', 'synthetic'),
        (w4, 'FINAL', 'DRAW', now() - interval '70 days' + interval '3 hours', now() - interval '70 days' + interval '3 hours', 'synthetic'),
        (w6, 'FINAL', 'HOME_WIN', now() - interval '3 days' + interval '3 hours', now() - interval '3 days' + interval '3 hours', 'synthetic');
    insert into proracle.results (fixture_id, status, actual_outcome, observed_at, finalized_at, source_name)
    values (w2, 'FINAL', 'HOME_WIN', now() - interval '10 days' + interval '3 hours', now() - interval '10 days' + interval '3 hours', 'synthetic')
    returning id into res2;
    insert into proracle.results (fixture_id, status, actual_outcome, observed_at, finalized_at, source_name, supersedes_result_id)
    values (w2, 'FINAL', 'AWAY_WIN', now() - interval '9 days', now() - interval '9 days', 'synthetic', res2);

    -- one ticket per fixture (fixture_count 1); t6 is complete too but its fixture is not settled, t5 is incomplete
    for rec in select * from (values (t1, w1), (t2, w2), (t3, w3), (t4, w4), (t5, w6), (t6, w5)) as x(tid, fid) loop
        d := d + 1;
        insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
        values (rec.tid, current_date - 100 - d, 'SIMULATED', 'verify-v1', 's', 1, 1, now() - interval '90 days');
        insert into proracle.ticket_fixtures (ticket_id, fixture_id, position) values (rec.tid, rec.fid, 1);
    end loop;
    -- picks: A (needs its prediction) and C on every ticket except t5, which has arm A only (incomplete)
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, source_prediction_id, kickoff_at, frozen_at)
    select x.tid, x.fid, 'A', 'verify-a', x.apick, x.pid, f.kickoff_at, f.kickoff_at - interval '2 days'
      from (values (t1, w1, 'HOME_WIN', pw1), (t2, w2, 'HOME_WIN', pw2), (t3, w3, 'DRAW', pw3),
                   (t4, w4, 'DRAW', pw4), (t5, w6, 'HOME_WIN', pw6), (t6, w5, 'HOME_WIN', pw5)) as x(tid, fid, apick, pid)
      join proracle.fixtures f on f.id = x.fid;
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, forced, kickoff_at, frozen_at)
    select x.tid, x.fid, 'C', 'verify-c', x.cpick, false, f.kickoff_at, f.kickoff_at - interval '2 days'
      from (values (t1, w1, 'DRAW'), (t2, w2, 'AWAY_WIN'), (t3, w3, 'HOME_WIN'), (t4, w4, 'HOME_WIN'), (t6, w5, 'DRAW')) as x(tid, fid, cpick)
      join proracle.fixtures f on f.id = x.fid;

    -- t7: two fixtures, arm A whole but arm C has only one of its two picks: a short arm, so not complete
    insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
    values (t7, current_date - 120, 'SIMULATED', 'verify-v1', 's', 2, 2, now() - interval '90 days');
    insert into proracle.ticket_fixtures (ticket_id, fixture_id, position) values (t7, w1, 1), (t7, w2, 2);
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, source_prediction_id, kickoff_at, frozen_at) values
        (t7, w1, 'A', 'verify-a', 'HOME_WIN', pw1, now() - interval '2 days', now() - interval '4 days'),
        (t7, w2, 'A', 'verify-a', 'HOME_WIN', pw2, now() - interval '10 days', now() - interval '12 days');
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, forced, kickoff_at, frozen_at) values
        (t7, w1, 'C', 'verify-c', 'DRAW', false, now() - interval '2 days', now() - interval '4 days');
    -- t8: arm C whole, arm A missing altogether: not complete
    insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
    values (t8, current_date - 121, 'SIMULATED', 'verify-v1', 's', 1, 1, now() - interval '90 days');
    insert into proracle.ticket_fixtures (ticket_id, fixture_id, position) values (t8, w1, 1);
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, forced, kickoff_at, frozen_at) values
        (t8, w1, 'C', 'verify-c', 'DRAW', false, now() - interval '2 days', now() - interval '4 days');
    -- t9: complete, two fixtures at different kickoffs (1 day and 20 days ago), with its own arm versions so its rows
    -- can be told apart: arm A right on the newer fixture and wrong on the older one
    insert into proracle.fixtures (id, home_team, away_team, kickoff_at, source_name, source_event_key) values
        (w7, 'P7', 'Q7', now() - interval '1 day', 'synthetic', 'wt-7'),
        (w8, 'P8', 'Q8', now() - interval '20 days', 'synthetic', 'wt-8');
    insert into proracle.predictions (
        id, fixture_id, contract_version, predicted_at, kickoff_at, model_provider, model_name,
        model_version, prompt_version, input_schema_version, input_snapshot_hash,
        home_win_probability, draw_probability, away_win_probability
    ) values
        (pw7, w7, '1.0', now() - interval '2 days', now() - interval '1 day', 'synthetic', 'test-model', '1', 'p', 'features-v1', repeat('7', 64), 0.5, 0.3, 0.2),
        (pw8, w8, '1.0', now() - interval '21 days', now() - interval '20 days', 'synthetic', 'test-model', '1', 'p', 'features-v1', repeat('8', 64), 0.5, 0.3, 0.2);
    insert into proracle.results (fixture_id, status, actual_outcome, observed_at, finalized_at, source_name) values
        (w7, 'FINAL', 'HOME_WIN', now() - interval '1 day' + interval '3 hours', now() - interval '1 day' + interval '3 hours', 'synthetic'),
        (w8, 'FINAL', 'DRAW', now() - interval '20 days' + interval '3 hours', now() - interval '20 days' + interval '3 hours', 'synthetic');
    insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
    values (t9, current_date - 122, 'SIMULATED', 'verify-v1', 's', 2, 2, now() - interval '90 days');
    insert into proracle.ticket_fixtures (ticket_id, fixture_id, position) values (t9, w7, 1), (t9, w8, 2);
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, source_prediction_id, kickoff_at, frozen_at) values
        (t9, w7, 'A', 'xcheck-a', 'HOME_WIN', pw7, now() - interval '1 day', now() - interval '3 days'),
        (t9, w8, 'A', 'xcheck-a', 'HOME_WIN', pw8, now() - interval '20 days', now() - interval '22 days');
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, forced, kickoff_at, frozen_at) values
        (t9, w7, 'C', 'xcheck-c', 'DRAW', false, now() - interval '1 day', now() - interval '3 days'),
        (t9, w8, 'C', 'xcheck-c', 'DRAW', false, now() - interval '20 days', now() - interval '22 days');

    execute 'set local role service_role';
    -- t9 (arm versions xcheck-): LAST7 holds only the newer fixture (1 settled, 1 right), LAST28 both (2 settled, 1 right),
    -- PRIOR28 nothing; arm C was right only on the older one; and its ticket row carries the newer kickoff as its latest
    select count(*) into n from proracle.intake_window_summary
     where arm_version = 'xcheck-a' and ((window_name = 'LAST7' and (picks_settled, picks_right, a_right_same_fixtures) = (1, 1, 1))
        or (window_name = 'LAST28' and (picks_settled, picks_right, a_right_same_fixtures) = (2, 1, 1)));
    if n <> 2 then raise exception 'ticket t9 arm A windows wrong'; end if;
    select count(*) into n from proracle.intake_window_summary where arm_version = 'xcheck-a' and window_name = 'PRIOR28';
    if n <> 0 then raise exception 'ticket t9 must not reach PRIOR28'; end if;
    select count(*) into n from proracle.intake_window_summary
     where arm_version = 'xcheck-c' and ((window_name = 'LAST7' and (picks_settled, picks_right, a_right_same_fixtures) = (1, 0, 1))
        or (window_name = 'LAST28' and (picks_settled, picks_right, a_right_same_fixtures) = (2, 1, 1)));
    if n <> 2 then raise exception 'ticket t9 arm C windows wrong'; end if;
    select count(*) into n from proracle.intake_ticket_hits h
     where ticket_id = t9 and arm = 'A' and picks_settled = 2 and picks_right = 1 and fixture_count = 2
       and last_kickoff_at between now() - interval '1 day 1 minute' and now() - interval '1 day' + interval '1 minute';
    if n <> 1 then raise exception 'ticket t9 hits wrong (latest kickoff must be the newer fixture)'; end if;
    -- windows. Expected, for arms A and C (verify- versions only):
    --   LAST7:   A 1 settled 1 right 1 A-right | C 1 settled 0 right 1 A-right            (t1)
    --   LAST28:  A 2 settled 1 right 1 A-right | C 2 settled 1 right 1 A-right            (t1, t2; t2's corrected result is AWAY_WIN)
    --   PRIOR28: A 1 settled 1 right 1 A-right | C 1 settled 0 right 1 A-right            (t3)
    for got in select arm, window_name, picks_settled, picks_right, a_right_same_fixtures from proracle.intake_window_summary
                where ticket_kind = 'SIMULATED' and arm_version like 'verify-%' order by arm, window_name loop
        if (got.arm, got.window_name) = ('A', 'LAST7') and (got.picks_settled, got.picks_right, got.a_right_same_fixtures) <> (1, 1, 1) then raise exception 'A LAST7 wrong: %', got; end if;
        if (got.arm, got.window_name) = ('A', 'LAST28') and (got.picks_settled, got.picks_right, got.a_right_same_fixtures) <> (2, 1, 1) then raise exception 'A LAST28 wrong: %', got; end if;
        if (got.arm, got.window_name) = ('A', 'PRIOR28') and (got.picks_settled, got.picks_right, got.a_right_same_fixtures) <> (1, 1, 1) then raise exception 'A PRIOR28 wrong: %', got; end if;
        if (got.arm, got.window_name) = ('C', 'LAST7') and (got.picks_settled, got.picks_right, got.a_right_same_fixtures) <> (1, 0, 1) then raise exception 'C LAST7 wrong: %', got; end if;
        if (got.arm, got.window_name) = ('C', 'LAST28') and (got.picks_settled, got.picks_right, got.a_right_same_fixtures) <> (2, 1, 1) then raise exception 'C LAST28 wrong: %', got; end if;
        if (got.arm, got.window_name) = ('C', 'PRIOR28') and (got.picks_settled, got.picks_right, got.a_right_same_fixtures) <> (1, 0, 1) then raise exception 'C PRIOR28 wrong: %', got; end if;
    end loop;
    select count(*) into n from proracle.intake_window_summary where ticket_kind = 'SIMULATED' and arm_version like 'verify-%';
    if n <> 6 then raise exception 'expected 6 window rows (arms A and C in three windows), got %', n; end if;

    -- one row per complete ticket and arm with settled picks: t1, t2, t3 x A, C (t4 is older than 56 days,
    -- t5 is incomplete, t6 is not settled)
    for got in select ticket_id, arm, fixture_count, picks_settled, picks_right, last_kickoff_at from proracle.intake_ticket_hits
                where ticket_kind = 'SIMULATED' and arm_version like 'verify-%' order by ticket_id, arm loop
        if got.fixture_count <> 1 or got.picks_settled <> 1 then raise exception 'ticket hits counts wrong: %', got; end if;
        if (got.ticket_id, got.arm) = (t1, 'A') and got.picks_right <> 1 then raise exception 't1 A wrong'; end if;
        if (got.ticket_id, got.arm) = (t1, 'C') and got.picks_right <> 0 then raise exception 't1 C wrong'; end if;
        if (got.ticket_id, got.arm) = (t2, 'A') and got.picks_right <> 0 then raise exception 't2 A wrong (corrected result)'; end if;
        if (got.ticket_id, got.arm) = (t2, 'C') and got.picks_right <> 1 then raise exception 't2 C wrong (corrected result)'; end if;
        if (got.ticket_id, got.arm) = (t3, 'A') and got.picks_right <> 1 then raise exception 't3 A wrong'; end if;
        if (got.ticket_id, got.arm) = (t3, 'C') and got.picks_right <> 0 then raise exception 't3 C wrong'; end if;
        if got.ticket_id in (t4, t5, t6) then raise exception 'a ticket that should be left out appeared: %', got; end if;
    end loop;
    select count(*) into n from proracle.intake_ticket_hits where ticket_kind = 'SIMULATED' and arm_version like 'verify-%';
    if n <> 6 then raise exception 'expected 6 ticket rows, got %', n; end if;
    select count(*) into n from proracle.intake_ticket_hits h
     where ticket_id = t1 and last_kickoff_at between now() - interval '2 days 1 minute' and now() - interval '2 days' + interval '1 minute';
    if n <> 2 then raise exception 'last_kickoff_at must be the latest settled kickoff'; end if;
    execute 'reset role';

    -- privileges: service_role reads, nobody writes, nobody else reads, both views run as the caller
    for rec in select unnest(array['intake_window_summary', 'intake_ticket_hits']) as v loop
        if not has_table_privilege('service_role', 'proracle.' || rec.v, 'SELECT') then raise exception 'service_role cannot read %', rec.v; end if;
        if has_table_privilege('service_role', 'proracle.' || rec.v, 'INSERT') or has_table_privilege('service_role', 'proracle.' || rec.v, 'UPDATE')
           or has_table_privilege('service_role', 'proracle.' || rec.v, 'DELETE') then raise exception 'service_role may write %', rec.v; end if;
        if has_table_privilege('anon', 'proracle.' || rec.v, 'SELECT') or has_table_privilege('authenticated', 'proracle.' || rec.v, 'SELECT')
           or has_table_privilege('public', 'proracle.' || rec.v, 'SELECT') or has_table_privilege('proracle_runtime', 'proracle.' || rec.v, 'SELECT') then
            raise exception 'a role that should not read % can', rec.v;
        end if;
        if not coalesce((select 'security_invoker=true' = any (c.reloptions) from pg_class c where c.oid = ('proracle.' || rec.v)::regclass), false) then
            raise exception '% must run as the caller', rec.v;
        end if;
    end loop;
    execute 'set local role proracle_runtime';
    begin
        perform 1 from proracle.intake_ticket_hits;
        raise exception 'the runtime role read a view';
    exception when insufficient_privilege then null;
    end;
    execute 'reset role';
end
$verify$;

rollback;
select 'verify_window_ticket_views: all checks passed' as result;
