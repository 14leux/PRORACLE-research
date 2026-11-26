-- Run via psql against a disposable local Supabase database after `supabase db reset --local`.
-- Verifies the multi-rule migration (20261128000000): several arm B picks per fixture told apart by
-- arm_version while arms A and C stay one per fixture, the active-rules and pending-retirement views,
-- the per-rule scoreboard, the new event and audit values, and the privileges. Rolls back everything
-- it writes. Any failed expectation raises an exception and stops psql (use -v ON_ERROR_STOP=1).
begin;

do $verify$
declare
    f1 constant uuid := '00000000-0000-4000-8000-0000000000f1';
    pred constant uuid := '00000000-0000-4000-8000-0000000000c1';
    tk constant uuid := '00000000-0000-4000-8000-0000000000d1';
    rule_a constant uuid := '00000000-0000-4000-8000-0000000000e1';
    rule_b constant uuid := '00000000-0000-4000-8000-0000000000e2';
    r record;
    got text;
begin
    insert into proracle.fixtures (id, home_team, away_team, kickoff_at, source_name, source_event_key)
    values (f1, 'Alpha', 'Beta', '2030-01-01 18:00:00+00', 'synthetic', 'mr-1');
    insert into proracle.predictions (
        id, fixture_id, contract_version, predicted_at, kickoff_at, model_provider, model_name,
        model_version, prompt_version, input_schema_version, input_snapshot_hash,
        home_win_probability, draw_probability, away_win_probability
    ) values (
        pred, f1, '1.0', '2030-01-01 12:00:00+00', '2030-01-01 18:00:00+00', 'synthetic', 'test-model', '1',
        'test-prompt-v1', 'features-v1', repeat('a', 64), 0.5, 0.3, 0.2
    );
    insert into proracle.results (id, fixture_id, status, actual_outcome, observed_at, finalized_at, source_name)
    values ('00000000-0000-4000-8000-0000000000a1', f1, 'FINAL', 'HOME_WIN', '2030-01-01 21:00:00+00', '2030-01-01 21:00:00+00', 'synthetic');

    -- 1. one ticket fixture, arms A and C and two rules' arm B picks ------------------------------------
    insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
    values (tk, '2030-01-01', 'SIMULATED', 'sim-v1', 's', 1, 1, '2030-01-01 06:00:00+00');
    insert into proracle.ticket_fixtures (ticket_id, fixture_id, position) values (tk, f1, 1);
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, source_prediction_id, kickoff_at, frozen_at)
    values (tk, f1, 'A', 'test-model', 'HOME_WIN', pred, '2030-01-01 18:00:00+00', '2030-01-01 06:00:00+00');
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, forced, kickoff_at, frozen_at)
    values (tk, f1, 'C', 'draw-score-v1', 'DRAW', true, '2030-01-01 18:00:00+00', '2030-01-01 06:00:00+00');
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, adjustment, kickoff_at, frozen_at) values
        (tk, f1, 'B', 'rule-v1', 'HOME_WIN', null, '2030-01-01 18:00:00+00', '2030-01-01 06:00:00+00'),
        (tk, f1, 'B', 'rule-v2', 'DRAW', 'FORCED_DRAW', '2030-01-01 18:00:00+00', '2030-01-01 06:00:00+00');

    begin
        insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, kickoff_at, frozen_at)
        values (tk, f1, 'B', 'rule-v1', 'AWAY_WIN', '2030-01-01 18:00:00+00', '2030-01-01 06:00:00+00');
        raise exception 'a second pick of the same rule on one fixture was accepted';
    exception when unique_violation then null;
    end;
    begin
        insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, source_prediction_id, kickoff_at, frozen_at)
        values (tk, f1, 'A', 'test-model-2', 'HOME_WIN', pred, '2030-01-01 18:00:00+00', '2030-01-01 06:00:00+00');
        raise exception 'a second arm A pick on one fixture was accepted';
    exception when unique_violation then null;
    end;
    begin
        insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, kickoff_at, frozen_at)
        values (tk, f1, 'C', 'draw-score-v2', 'DRAW', '2030-01-01 18:00:00+00', '2030-01-01 06:00:00+00');
        raise exception 'a second arm C pick on one fixture was accepted';
    exception when unique_violation then null;
    end;

    -- 2. the per-rule scoreboard (the fixture finished HOME_WIN) -----------------------------------------
    execute 'set local role service_role';
    for r in select * from proracle.intake_scoreboard where ticket_kind = 'SIMULATED' order by arm, arm_version loop
        got := concat_ws(',', r.arm, r.arm_version, r.picks_settled, r.picks_right, r.a_right_same_fixtures,
                         r.c_right_same_fixtures, r.adjusted_settled, r.adjusted_right, r.a_right_on_adjusted);
        if r.arm = 'A' and got <> 'A,test-model,1,1,1,0,0,0,0' then raise exception 'arm A row wrong: %', got; end if;
        if r.arm_version = 'rule-v1' and got <> 'B,rule-v1,1,1,1,0,0,0,0' then raise exception 'rule v1 row wrong: %', got; end if;
        if r.arm_version = 'rule-v2' and got <> 'B,rule-v2,1,0,1,0,1,0,1' then raise exception 'rule v2 row wrong: %', got; end if;
        if r.arm = 'C' and got <> 'C,draw-score-v1,1,0,1,0,1,0,1' then raise exception 'arm C row wrong: %', got; end if;
    end loop;
    if (select count(*) from proracle.intake_scoreboard where ticket_kind = 'SIMULATED') <> 4 then
        raise exception 'the scoreboard should have one row each for A, C and the two rules';
    end if;
    execute 'reset role';

    -- 3. active rules and pending retirement -----------------------------------------------------------
    insert into proracle.intake_rules (id, version, structure, source_text) values
        (rule_a, 1, '{"v":1}', 'a'), (rule_b, 2, '{"v":1}', 'b');
    insert into proracle.intake_rule_events (rule_id, event) values
        (rule_a, 'PROPOSED'), (rule_a, 'ACTIVATED'), (rule_b, 'PROPOSED'), (rule_b, 'ACTIVATED');
    execute 'set local role service_role';
    if (select string_agg(version::text, ',' order by version) from proracle.intake_active_rules) <> '1,2' then
        raise exception 'both activated rules should be active at once';
    end if;
    execute 'reset role';
    -- the Python workflow reads them as the restricted runtime role (row-level policies included)
    execute 'set local role proracle_runtime';
    if (select string_agg(version::text, ',' order by version) from proracle.intake_active_rules) <> '1,2' then
        raise exception 'the runtime role should read both active rules';
    end if;
    execute 'reset role';
    insert into proracle.intake_rule_events (rule_id, event) values (rule_a, 'RETIRE_REQUESTED');
    execute 'set local role service_role';
    if (select string_agg(version::text, ',' order by version) from proracle.intake_active_rules) <> '1,2' then
        raise exception 'a retirement request must not stop a rule';
    end if;
    if (select version from proracle.intake_pending_retirement) is distinct from 1 then
        raise exception 'the retirement request should be pending';
    end if;
    execute 'reset role';
    insert into proracle.intake_rule_events (rule_id, event) values (rule_a, 'RETIRE_DECLINED');
    execute 'set local role service_role';
    if exists (select 1 from proracle.intake_pending_retirement) then
        raise exception 'a declined retirement must not stay pending';
    end if;
    execute 'reset role';
    insert into proracle.intake_rule_events (rule_id, event) values (rule_b, 'RETIRE_REQUESTED'), (rule_b, 'RETIRED');
    execute 'set local role service_role';
    if (select string_agg(version::text, ',' order by version) from proracle.intake_active_rules) <> '1' then
        raise exception 'a confirmed retirement should leave only the other rule active';
    end if;
    insert into proracle.intake_audit (action, outcome, detail) values ('RULE_RETIRE_REQUESTED', 'OK', 'v2'), ('RULE_RETIRED', 'OK', 'v2');
    execute 'reset role';

    -- 4. privileges ----------------------------------------------------------------------------------
    if not has_table_privilege('service_role', 'proracle.intake_active_rules', 'select')
       or not has_table_privilege('service_role', 'proracle.intake_pending_retirement', 'select')
       or not has_table_privilege('service_role', 'proracle.intake_scoreboard', 'select') then
        raise exception 'service_role must read the three views';
    end if;
    if not has_table_privilege('proracle_runtime', 'proracle.intake_active_rules', 'select') then
        raise exception 'the runtime role must read the active rules';
    end if;
    if has_table_privilege('proracle_runtime', 'proracle.intake_scoreboard', 'select')
       or has_table_privilege('proracle_runtime', 'proracle.intake_pending_retirement', 'select')
       or has_table_privilege('anon', 'proracle.intake_active_rules', 'select')
       or has_table_privilege('authenticated', 'proracle.intake_scoreboard', 'select') then
        raise exception 'no other role may read these views';
    end if;
    if has_table_privilege('service_role', 'proracle.intake_active_rules', 'insert, update, delete') then
        raise exception 'the views are read-only';
    end if;
end
$verify$;

rollback;
select 'verify_multi_rule: all checks passed' as result;
