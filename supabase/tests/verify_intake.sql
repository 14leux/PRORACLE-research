-- Run via psql against a disposable local Supabase database after `supabase db reset --local`.
-- Verifies the intake migration (20261127000000): privileges, the daily call cap, constraints, the
-- rule lifecycle views, screenshot tickets and the arm scoreboard. Rolls back everything it writes.
-- Any failed expectation raises an exception and stops psql (use -v ON_ERROR_STOP=1).
begin;

do $verify$
declare
    append_only constant text[] := array[
        'intake_updates', 'intake_tickets', 'intake_ticket_rows', 'intake_rules',
        'intake_rule_events', 'intake_audit'
    ];
    all_tables constant text[] := array[
        'intake_updates', 'intake_usage', 'intake_tickets', 'intake_ticket_rows', 'intake_rules',
        'intake_rule_events', 'intake_audit'
    ];
    views constant text[] := array['intake_active_rule', 'intake_pending_rule', 'intake_arm_scoreboard'];
    runtime_reads constant text[] := array[
        'intake_tickets', 'intake_ticket_rows', 'intake_rules', 'intake_rule_events', 'intake_active_rule'
    ];
    t text;
    r text;
    n bigint;
    m bigint;
    k bigint;
    f1 constant uuid := '00000000-0000-4000-8000-0000000000f1';
    f2 constant uuid := '00000000-0000-4000-8000-0000000000f2';
    f3 constant uuid := '00000000-0000-4000-8000-0000000000f3';
    tk1 constant uuid := '00000000-0000-4000-8000-0000000000d1';
    tk2 constant uuid := '00000000-0000-4000-8000-0000000000d2';
    tk3 constant uuid := '00000000-0000-4000-8000-0000000000d3';
    rule1 constant uuid := '00000000-0000-4000-8000-0000000000e1';
    rule2 constant uuid := '00000000-0000-4000-8000-0000000000e2';
    rule3 constant uuid := '00000000-0000-4000-8000-0000000000e3';
    rule4 constant uuid := '00000000-0000-4000-8000-0000000000e4';
    stmt text;
    first_col text;
    intake1 uuid;
    intake2 uuid;
begin
    -- 1. Structure and privileges -----------------------------------------------------------------
    foreach t in array all_tables loop
        if not coalesce((select c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
                          where n.nspname = 'proracle' and c.relname = t), false) then
            raise exception 'RLS is not enabled on proracle.%', t;
        end if;
    end loop;
    foreach t in array all_tables || views loop
        foreach r in array array['anon', 'authenticated'] loop
            if has_any_column_privilege(r, format('proracle.%I', t), 'SELECT')
               or has_any_column_privilege(r, format('proracle.%I', t), 'INSERT')
               or has_any_column_privilege(r, format('proracle.%I', t), 'UPDATE')
               or has_table_privilege(r, format('proracle.%I', t), 'DELETE')
               or has_table_privilege(r, format('proracle.%I', t), 'TRUNCATE') then
                raise exception 'Unexpected API role access: % on proracle.%', r, t;
            end if;
        end loop;
        if has_table_privilege('service_role', format('proracle.%I', t), 'DELETE')
           or has_table_privilege('service_role', format('proracle.%I', t), 'TRUNCATE') then
            raise exception 'service_role can delete or truncate proracle.%', t;
        end if;
    end loop;
    foreach t in array append_only loop
        if not has_table_privilege('service_role', format('proracle.%I', t), 'SELECT')
           or not has_table_privilege('service_role', format('proracle.%I', t), 'INSERT')
           or has_any_column_privilege('service_role', format('proracle.%I', t), 'UPDATE') then
            raise exception 'service_role must have exactly select and insert on proracle.%', t;
        end if;
    end loop;
    if not has_table_privilege('service_role', 'proracle.intake_usage', 'SELECT')
       or not has_table_privilege('service_role', 'proracle.intake_usage', 'INSERT')
       or not has_column_privilege('service_role', 'proracle.intake_usage', 'vision_calls', 'UPDATE')
       or not has_column_privilege('service_role', 'proracle.intake_usage', 'updated_at', 'UPDATE')
       or has_column_privilege('service_role', 'proracle.intake_usage', 'usage_date', 'UPDATE') then
        raise exception 'service_role privileges on intake_usage are not select, insert and update of the two counter columns';
    end if;
    foreach t in array views loop
        if not has_table_privilege('service_role', format('proracle.%I', t), 'SELECT') then
            raise exception 'service_role cannot read proracle.%', t;
        end if;
    end loop;
    foreach t in array all_tables || views loop
        if has_any_column_privilege('proracle_runtime', format('proracle.%I', t), 'INSERT')
           or has_any_column_privilege('proracle_runtime', format('proracle.%I', t), 'UPDATE')
           or has_table_privilege('proracle_runtime', format('proracle.%I', t), 'DELETE')
           or has_table_privilege('proracle_runtime', format('proracle.%I', t), 'TRUNCATE') then
            raise exception 'proracle_runtime can write proracle.%', t;
        end if;
        if (t = any (runtime_reads)) <> has_table_privilege('proracle_runtime', format('proracle.%I', t), 'SELECT') then
            raise exception 'proracle_runtime read access to proracle.% is not as intended', t;
        end if;
    end loop;
    if not has_function_privilege('service_role', 'proracle.intake_reserve_call(date, integer)', 'EXECUTE')
       or has_function_privilege('proracle_runtime', 'proracle.intake_reserve_call(date, integer)', 'EXECUTE')
       or has_function_privilege('anon', 'proracle.intake_reserve_call(date, integer)', 'EXECUTE')
       or has_function_privilege('authenticated', 'proracle.intake_reserve_call(date, integer)', 'EXECUTE') then
        raise exception 'execute on intake_reserve_call is not limited to service_role';
    end if;

    -- 2. Synthetic fixtures (owner) -----------------------------------------------------------------
    insert into proracle.fixtures (id, home_team, away_team, kickoff_at, source_name, source_event_key) values
        (f1, 'Alpha', 'Beta',  '2030-01-01 18:00:00+00', 'synthetic', 'in-1'),
        (f2, 'Gamma', 'Delta', '2030-01-01 19:00:00+00', 'synthetic', 'in-2'),
        (f3, 'Eps',   'Zeta',  '2030-01-01 20:00:00+00', 'synthetic', 'in-3');

    -- 3. service_role: the daily call cap, intake records, constraints ---------------------------------
    execute 'set local role service_role';
    if not (proracle.intake_reserve_call('2030-01-01', 2) and proracle.intake_reserve_call('2030-01-01', 2)) then
        raise exception 'the first two calls of the day must be allowed';
    end if;
    if proracle.intake_reserve_call('2030-01-01', 2) then
        raise exception 'the third call must be refused at a cap of 2';
    end if;
    if not proracle.intake_reserve_call('2030-01-02', 2) then
        raise exception 'a new UTC day starts again';
    end if;
    if proracle.intake_reserve_call('2030-01-03', 0) or proracle.intake_reserve_call('2030-01-03', null) then
        raise exception 'a cap below 1 must refuse every call';
    end if;
    if (select vision_calls from proracle.intake_usage where usage_date = '2030-01-01') <> 2 then
        raise exception 'the counter must stop at the cap';
    end if;

    insert into proracle.intake_updates (update_id, kind) values (101, 'IMAGE'), (102, 'PDF'), (103, 'COMMAND');
    insert into proracle.intake_tickets (update_id, ticket_date, source_kind, bookmaker_label, vision_model, rows_read, rows_kept, rows_matched)
    values (101, '2030-01-01', 'IMAGE', 'BookX', 'claude-sonnet-5-5', 3, 3, 2)
    returning id into intake1;
    insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched)
    values (102, '2030-01-01', 'PDF', 'claude-sonnet-5-5', 1, 1, 1)
    returning id into intake2;
    insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, kickoff_at, home_odds, draw_odds, away_odds, matched_fixture_id)
    values (intake1, 1, 'Alpha', 'Beta', '2030-01-01 18:00:00+00', 2.10, 3.30, 3.40, f1),
           (intake1, 2, 'Gamma', 'Delta', '2030-01-01 19:00:00+00', 1.50, 4.00, 6.00, f2),
           (intake1, 3, 'Unknown FC', 'Nobody', '2030-01-01 20:00:00+00', null, null, null, null);
    insert into proracle.intake_audit (update_id, action, outcome, detail) values (101, 'TICKET', 'OK', 'rows=3 matched=2');

    foreach stmt in array array[
        $s$insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, home_odds, draw_odds)
           values (%L, 4, 'A', 'B', 2.0, 3.0)$s$,
        $s$insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team)
           values (%L, 21, 'A', 'B')$s$,
        $s$insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, home_odds, draw_odds, away_odds)
           values (%L, 5, 'A', 'B', 1.0, 3.0, 3.0)$s$,
        $s$insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team)
           values (%L, 6, repeat('x', 61), 'B')$s$,
        $s$insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team)
           values (%L, 1, 'Dup', 'Position')$s$
    ] loop
        begin
            execute format(stmt, intake1);
            raise exception 'constraint did not fire for: %', left(stmt, 100);
        exception when check_violation or unique_violation then null;
        end;
    end loop;
    foreach stmt in array array[
        $s$insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched)
           values (103, '2030-01-01', 'IMAGE', 'm', 25, 21, 0)$s$,
        $s$insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched)
           values (103, '2030-01-01', 'IMAGE', 'm', 3, 2, 3)$s$,
        $s$insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched)
           values (103, '2030-01-01', 'GIF', 'm', 1, 1, 1)$s$,
        $s$insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched)
           values (101, '2030-01-01', 'IMAGE', 'm', 1, 1, 1)$s$,
        $s$insert into proracle.intake_audit (action, outcome) values ('SOMETHING_ELSE', 'OK')$s$,
        $s$insert into proracle.intake_audit (action, outcome, detail) values ('ERROR', 'ERROR', repeat('x', 201))$s$,
        $s$insert into proracle.intake_updates (update_id, kind) values (104, 'VIDEO')$s$
    ] loop
        begin
            execute stmt;
            raise exception 'constraint did not fire for: %', left(stmt, 100);
        exception when check_violation or unique_violation then null;
        end;
    end loop;

    -- service_role cannot change or remove what it wrote, except the daily counter ------------------
    foreach t in array append_only loop
        select column_name into first_col from information_schema.columns
         where table_schema = 'proracle' and table_name = t order by ordinal_position desc limit 1;
        foreach stmt in array array[
            format('update proracle.%I set %I = %I', t, first_col, first_col),
            format('delete from proracle.%I', t),
            format('truncate proracle.%I', t)
        ] loop
            begin
                execute stmt;
                raise exception 'service_role was allowed to run: %', stmt;
            exception when insufficient_privilege then null;
            end;
        end loop;
    end loop;
    begin
        execute 'update proracle.intake_usage set usage_date = usage_date + 1';
        raise exception 'service_role changed usage_date';
    exception when insufficient_privilege then null;
    end;
    begin
        execute 'delete from proracle.intake_usage';
        raise exception 'service_role deleted usage';
    exception when insufficient_privilege then null;
    end;

    -- 4. Rule lifecycle: propose, confirm, replace, decline, expire --------------------------------------
    insert into proracle.intake_rules (id, version, structure, source_text) values
        (rule1, 1, '{"v":1,"force_draws":{"count":5,"basis":"draw_score"}}', '5 draws');
    insert into proracle.intake_rule_events (rule_id, event, update_id) values (rule1, 'PROPOSED', 103);
    if (select version from proracle.intake_pending_rule) is distinct from 1 or exists (select 1 from proracle.intake_active_rule) then
        raise exception 'a proposed rule must be pending and not active';
    end if;
    insert into proracle.intake_rule_events (rule_id, event, update_id) values (rule1, 'ACTIVATED', 103);
    if (select version from proracle.intake_active_rule) is distinct from 1 or exists (select 1 from proracle.intake_pending_rule) then
        raise exception 'a confirmed rule must be active and no longer pending';
    end if;
    insert into proracle.intake_rules (id, version, structure, source_text) values
        (rule2, 2, '{"v":1,"reverse":{"count":4,"basis":"margin"}}', '4 upsets');
    insert into proracle.intake_rule_events (rule_id, event) values (rule2, 'PROPOSED');
    if (select version from proracle.intake_active_rule) is distinct from 1 then
        raise exception 'a pending proposal must not replace the active rule';
    end if;
    insert into proracle.intake_rule_events (rule_id, event) values (rule2, 'ACTIVATED'), (rule1, 'RETIRED');
    if (select count(*) from proracle.intake_active_rule) <> 1 or (select version from proracle.intake_active_rule) <> 2 then
        raise exception 'activating v2 and retiring v1 must leave exactly v2 active';
    end if;
    insert into proracle.intake_rules (id, version, structure, source_text) values
        (rule3, 3, '{"v":1,"skip_leagues":["X"]}', 'skip X');
    insert into proracle.intake_rule_events (rule_id, event) values (rule3, 'PROPOSED'), (rule3, 'DECLINED');
    if exists (select 1 from proracle.intake_pending_rule) or (select version from proracle.intake_active_rule) <> 2 then
        raise exception 'a declined rule must be neither pending nor active';
    end if;
    insert into proracle.intake_rules (id, version, structure, source_text) values
        (rule4, 4, '{"v":1,"only_leagues":["Y"]}', 'only Y');
    insert into proracle.intake_rule_events (rule_id, event, created_at) values (rule4, 'PROPOSED', now() - interval '2 hours');
    if exists (select 1 from proracle.intake_pending_rule) then
        raise exception 'a proposal older than one hour must have expired';
    end if;
    begin
        insert into proracle.intake_rules (version, structure, source_text) values (2, '{}', 'dup version');
        raise exception 'duplicate rule version accepted';
    exception when unique_violation then null;
    end;
    begin
        insert into proracle.intake_rules (version, structure, source_text) values (9, '[]', 'not an object');
        raise exception 'non-object rule structure accepted';
    exception when check_violation then null;
    end;

    -- 5. runtime role: reads what it needs, writes nothing in the intake tables --------------------------
    execute 'reset role';
    execute 'set local role proracle_runtime';
    if (select count(*) from proracle.intake_ticket_rows) <> 3 or (select version from proracle.intake_active_rule) <> 2 then
        raise exception 'runtime role cannot read the intake rows or the active rule';
    end if;
    foreach stmt in array array[
        'select 1 from proracle.intake_audit',
        'select 1 from proracle.intake_usage',
        'select 1 from proracle.intake_updates',
        'select 1 from proracle.intake_pending_rule',
        'insert into proracle.intake_updates (update_id, kind) values (999, ''TEXT'')',
        'update proracle.intake_rules set version = version',
        'delete from proracle.intake_rule_events'
    ] loop
        begin
            execute stmt;
            raise exception 'runtime role was allowed to run: %', stmt;
        exception when insufficient_privilege then null;
        end;
    end loop;

    -- screenshot tickets, written as the runtime role (as ticket_mode does) ----------------------------------
    insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at, intake_id)
    values (tk1, '2030-01-01', 'SCREENSHOT', 'shot-v1', 'seed', 2, 2, '2030-01-01 12:00:00+00', intake1),
           (tk2, '2030-01-01', 'SCREENSHOT', 'shot-v1', 'seed', 1, 1, '2030-01-01 12:00:00+00', intake2);
    insert into proracle.ticket_fixtures (ticket_id, fixture_id, position) values
        (tk1, f1, 1), (tk1, f2, 2), (tk2, f3, 1);
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, adjustment, kickoff_at, frozen_at) values
        (tk1, f1, 'B', 'rule-v2', 'AWAY_WIN', 'REVERSED', '2030-01-01 18:00:00+00', '2030-01-01 12:00:00+00'),
        (tk1, f2, 'B', 'rule-v2', 'DRAW', 'FORCED_DRAW', '2030-01-01 19:00:00+00', '2030-01-01 12:00:00+00');
    foreach stmt in array array[
        -- a screenshot ticket needs its intake, and a simulated one must not have one
        $s$insert into proracle.tickets (ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
           values ('2030-01-02', 'SCREENSHOT', 'shot-v1', 's', 1, 1, '2030-01-01 12:00:00+00')$s$,
        format($s$insert into proracle.tickets (ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at, intake_id)
           values ('2030-01-02', 'SIMULATED', 'sim-v1', 's', 1, 1, '2030-01-01 12:00:00+00', %L)$s$, intake1),
        -- one ticket per intake read
        format($s$insert into proracle.tickets (ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at, intake_id)
           values ('2030-01-03', 'SCREENSHOT', 'shot-v1', 's', 1, 1, '2030-01-01 12:00:00+00', %L)$s$, intake1),
        -- an adjustment belongs to arm B only
        format($s$insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, adjustment, kickoff_at, frozen_at)
           values (%L, %L, 'C', 'draw-score-v1', 'DRAW', 'REVERSED', '2030-01-01 20:00:00+00', '2030-01-01 12:00:00+00')$s$, tk2, f3),
        $s$insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, adjustment, kickoff_at, frozen_at)
           values ('00000000-0000-4000-8000-0000000000d2', '00000000-0000-4000-8000-0000000000f3', 'B', 'r', 'DRAW', 'SIDEWAYS', '2030-01-01 20:00:00+00', '2030-01-01 12:00:00+00')$s$
    ] loop
        begin
            execute stmt;
            raise exception 'constraint did not fire for: %', left(stmt, 110);
        exception when check_violation or unique_violation or foreign_key_violation then null;
        end;
    end loop;
    -- the per-day rule for simulated tickets still holds
    insert into proracle.tickets (ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
    values ('2030-02-01', 'SIMULATED', 'sim-v1', 's', 20, 1, '2030-02-01 06:00:00+00');
    begin
        insert into proracle.tickets (ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
        values ('2030-02-01', 'SIMULATED', 'sim-v1', 's2', 20, 1, '2030-02-01 06:00:00+00');
        raise exception 'a second simulated ticket for the same day was accepted';
    exception when unique_violation then null;
    end;

    -- 6. arm scoreboard: complete tickets only, current results only ------------------------------------------
    execute 'reset role';
    insert into proracle.results (id, fixture_id, status, actual_outcome, observed_at, finalized_at, source_name) values
        ('00000000-0000-4000-8000-0000000000a1', f1, 'FINAL', 'AWAY_WIN', '2030-01-01 21:00:00+00', '2030-01-01 21:00:00+00', 'synthetic'),
        ('00000000-0000-4000-8000-0000000000a2', f2, 'FINAL', 'HOME_WIN', '2030-01-01 21:00:00+00', '2030-01-01 21:00:00+00', 'synthetic');
    -- a correction of f2 to a draw: the superseded HOME_WIN must no longer count
    insert into proracle.results (id, fixture_id, supersedes_result_id, status, actual_outcome, observed_at, finalized_at, source_name)
    values ('00000000-0000-4000-8000-0000000000a3', f2, '00000000-0000-4000-8000-0000000000a2', 'FINAL', 'DRAW',
            '2030-01-02 08:00:00+00', '2030-01-01 21:00:00+00', 'synthetic');
    -- an INCOMPLETE ticket (it says 3 fixtures but only 1 is stored) must not count, even with a wrong pick
    insert into proracle.intake_updates (update_id, kind) values (105, 'IMAGE');
    insert into proracle.intake_tickets (id, update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched)
    values ('00000000-0000-4000-8000-0000000000b3', 105, '2030-01-05', 'IMAGE', 'm', 3, 3, 1);
    insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at, intake_id)
    values (tk3, '2030-01-05', 'SCREENSHOT', 'shot-v1', 's', 3, 3, '2030-01-01 12:00:00+00', '00000000-0000-4000-8000-0000000000b3');
    insert into proracle.ticket_fixtures (ticket_id, fixture_id, position) values (tk3, f1, 1);
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, adjustment, kickoff_at, frozen_at)
    values (tk3, f1, 'B', 'rule-v2', 'HOME_WIN', 'FORCED_DRAW', '2030-01-01 18:00:00+00', '2030-01-01 12:00:00+00');
    execute 'set local role service_role';
    select picks_settled, picks_right, tickets into n, m, k
      from proracle.intake_arm_scoreboard where arm = 'B' and ticket_kind = 'SCREENSHOT';
    if n is distinct from 2 or m is distinct from 2 or k is distinct from 1 then
        raise exception 'arm B scoreboard should show 1 complete ticket, 2 settled picks, 2 right; got tickets=%, settled=%, right=%', k, n, m;
    end if;
    execute 'reset role';
end
$verify$;

rollback;
select 'verify_intake: all checks passed' as result;
