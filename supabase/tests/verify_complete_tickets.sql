-- Run via psql against a disposable local Supabase database after `supabase db reset --local`.
-- Verifies the complete-tickets migration (20261201000000, IMP-065): the scoreboard and the recent window
-- count a ticket only when every arm present holds one pick per fixture and arms A and C are both there.
-- A ticket with one arm B pick missing, with arm C missing, or with no picks at all is left out of both
-- views, and a complete ticket (with an arm B, or without one) is counted. Rolls back everything it writes.
-- Any failed expectation raises an exception and stops psql (use -v ON_ERROR_STOP=1).
begin;

do $verify$
declare
    g1 constant uuid := '00000000-0000-4000-8000-0000000000e1';
    g2 constant uuid := '00000000-0000-4000-8000-0000000000e2';
    pr1 constant uuid := '00000000-0000-4000-8000-0000000000e3';
    pr2 constant uuid := '00000000-0000-4000-8000-0000000000e4';
    tk_ok constant uuid := '00000000-0000-4000-8000-0000000000f1';
    tk_no_b constant uuid := '00000000-0000-4000-8000-0000000000f2';
    tk_short_b constant uuid := '00000000-0000-4000-8000-0000000000f3';
    tk_no_c constant uuid := '00000000-0000-4000-8000-0000000000f4';
    tk_none constant uuid := '00000000-0000-4000-8000-0000000000f5';
    tk_dup_extra constant uuid := '00000000-0000-4000-8000-0000000000f6';
    tk_no_a constant uuid := '00000000-0000-4000-8000-0000000000f7';
    t uuid;
    n int;
    d int := 0;
    got record;
begin
    insert into proracle.fixtures (id, home_team, away_team, kickoff_at, source_name, source_event_key) values
        (g1, 'E1', 'F1', now() - interval '5 days', 'synthetic', 'ct-1'),
        (g2, 'E2', 'F2', now() - interval '5 days', 'synthetic', 'ct-2');
    insert into proracle.predictions (
        id, fixture_id, contract_version, predicted_at, kickoff_at, model_provider, model_name,
        model_version, prompt_version, input_schema_version, input_snapshot_hash,
        home_win_probability, draw_probability, away_win_probability
    ) values
        (pr1, g1, '1.0', now() - interval '6 days', now() - interval '5 days', 'synthetic', 'test-model', '1', 'p', 'features-v1', repeat('a', 64), 0.5, 0.3, 0.2),
        (pr2, g2, '1.0', now() - interval '6 days', now() - interval '5 days', 'synthetic', 'test-model', '1', 'p', 'features-v1', repeat('b', 64), 0.5, 0.3, 0.2);
    insert into proracle.results (fixture_id, status, actual_outcome, observed_at, finalized_at, source_name) values
        (g1, 'FINAL', 'HOME_WIN', now() - interval '5 days' + interval '3 hours', now() - interval '5 days' + interval '3 hours', 'synthetic'),
        (g2, 'FINAL', 'DRAW', now() - interval '5 days' + interval '3 hours', now() - interval '5 days' + interval '3 hours', 'synthetic');

    -- six tickets of two fixtures each, one per day so the one-ticket-per-day key does not fire
    foreach t in array array[tk_ok, tk_no_b, tk_short_b, tk_no_c, tk_none, tk_dup_extra, tk_no_a] loop
        d := d + 1;
        insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
        values (t, current_date - 20 - d, 'SIMULATED', 'verify-v1', 's', 2, 2, now() - interval '7 days');
        insert into proracle.ticket_fixtures (ticket_id, fixture_id, position) values (t, g1, 1), (t, g2, 2);
    end loop;

    -- arms A and C on every ticket except tk_none; arm A needs its source prediction, arm C forces a draw
    foreach t in array array[tk_ok, tk_no_b, tk_short_b, tk_dup_extra] loop
        insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, source_prediction_id, kickoff_at, frozen_at) values
            (t, g1, 'A', 'verify-a', 'HOME_WIN', pr1, now() - interval '5 days', now() - interval '6 days'),
            (t, g2, 'A', 'verify-a', 'HOME_WIN', pr2, now() - interval '5 days', now() - interval '6 days');
        insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, forced, kickoff_at, frozen_at) values
            (t, g1, 'C', 'verify-c', 'DRAW', true, now() - interval '5 days', now() - interval '6 days'),
            (t, g2, 'C', 'verify-c', 'DRAW', true, now() - interval '5 days', now() - interval '6 days');
    end loop;
    -- tk_no_a: arms C and B only, each whole: arm A is missing altogether
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, forced, kickoff_at, frozen_at) values
        (tk_no_a, g1, 'C', 'verify-c', 'DRAW', true, now() - interval '5 days', now() - interval '6 days'),
        (tk_no_a, g2, 'C', 'verify-c', 'DRAW', true, now() - interval '5 days', now() - interval '6 days');
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, kickoff_at, frozen_at) values
        (tk_no_a, g1, 'B', 'verify-b', 'HOME_WIN', now() - interval '5 days', now() - interval '6 days'),
        (tk_no_a, g2, 'B', 'verify-b', 'DRAW', now() - interval '5 days', now() - interval '6 days');
    -- tk_no_c: arms A and B only
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, source_prediction_id, kickoff_at, frozen_at) values
        (tk_no_c, g1, 'A', 'verify-a', 'HOME_WIN', pr1, now() - interval '5 days', now() - interval '6 days'),
        (tk_no_c, g2, 'A', 'verify-a', 'HOME_WIN', pr2, now() - interval '5 days', now() - interval '6 days');
    -- arm B: complete on tk_ok and tk_no_c, one pick short on tk_short_b, none on tk_no_b
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, kickoff_at, frozen_at) values
        (tk_ok, g1, 'B', 'verify-b', 'HOME_WIN', now() - interval '5 days', now() - interval '6 days'),
        (tk_ok, g2, 'B', 'verify-b', 'DRAW', now() - interval '5 days', now() - interval '6 days'),
        (tk_no_c, g1, 'B', 'verify-b', 'HOME_WIN', now() - interval '5 days', now() - interval '6 days'),
        (tk_no_c, g2, 'B', 'verify-b', 'DRAW', now() - interval '5 days', now() - interval '6 days'),
        (tk_short_b, g1, 'B', 'verify-b', 'HOME_WIN', now() - interval '5 days', now() - interval '6 days');
    -- tk_dup_extra: arm B written for one fixture only, in a second version, with the first version complete:
    -- one short arm version spoils the ticket even though the other arm versions are whole
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, kickoff_at, frozen_at) values
        (tk_dup_extra, g1, 'B', 'verify-b', 'HOME_WIN', now() - interval '5 days', now() - interval '6 days'),
        (tk_dup_extra, g2, 'B', 'verify-b', 'DRAW', now() - interval '5 days', now() - interval '6 days'),
        (tk_dup_extra, g1, 'B', 'verify-b2', 'DRAW', now() - interval '5 days', now() - interval '6 days');

    execute 'set local role service_role';
    -- the scoreboard: tk_ok (with arm B) and tk_no_b (arms A and C only) are complete; the other four are not
    for got in select arm, arm_version, tickets, picks_settled from proracle.intake_scoreboard
                where ticket_kind = 'SIMULATED' and arm_version like 'verify-%' order by arm, arm_version loop
        if got.arm = 'A' and (got.tickets, got.picks_settled) <> (2, 4) then raise exception 'scoreboard arm A wrong: %', got; end if;
        if got.arm = 'C' and (got.tickets, got.picks_settled) <> (2, 4) then raise exception 'scoreboard arm C wrong: %', got; end if;
        if got.arm = 'B' and (got.tickets, got.picks_settled) <> (1, 2) then raise exception 'scoreboard arm B wrong: %', got; end if;
        if got.arm_version = 'verify-b2' then raise exception 'a short arm version must not appear'; end if;
    end loop;
    select count(*) into n from proracle.intake_scoreboard where ticket_kind = 'SIMULATED' and arm_version like 'verify-%';
    if n <> 3 then raise exception 'the scoreboard should list arms A, B and C once each, got % rows', n; end if;

    -- the recent window agrees (every pick is in the last 28 days)
    for got in select arm, window_name, picks_settled from proracle.intake_recent_window
                where ticket_kind = 'SIMULATED' and arm_version like 'verify-%' order by arm loop
        if got.window_name <> 'RECENT' then raise exception 'window wrong: %', got; end if;
        if got.arm in ('A', 'C') and got.picks_settled <> 4 then raise exception 'window arm % wrong: %', got.arm, got; end if;
        if got.arm = 'B' and got.picks_settled <> 2 then raise exception 'window arm B wrong: %', got; end if;
    end loop;
    select count(*) into n from proracle.intake_recent_window where ticket_kind = 'SIMULATED' and arm_version like 'verify-%';
    if n <> 3 then raise exception 'the window should list arms A, B and C once each, got % rows', n; end if;
    execute 'reset role';

    -- the readers keep their columns and grants
    if not has_table_privilege('service_role', 'proracle.intake_scoreboard', 'SELECT')
       or not has_table_privilege('service_role', 'proracle.intake_recent_window', 'SELECT') then
        raise exception 'service_role lost its read of a view';
    end if;
    if has_table_privilege('anon', 'proracle.intake_scoreboard', 'SELECT') or has_table_privilege('proracle_runtime', 'proracle.intake_recent_window', 'SELECT') then
        raise exception 'a view became readable by a role that should not read it';
    end if;
    if not (select bool_and('security_invoker=true' = any (c.reloptions)) from pg_class c
             where c.oid in ('proracle.intake_scoreboard'::regclass, 'proracle.intake_recent_window'::regclass)) then
        raise exception 'both views must run as the caller';
    end if;
end
$verify$;

rollback;
select 'verify_complete_tickets: all checks passed' as result;
