-- Run via psql against a disposable local Supabase database after `supabase db reset --local`.
-- Verifies the P0 capture migration (match_scores, competition_countries, tickets,
-- ticket_fixtures, ticket_arm_picks). The transaction rolls back every row it inserts.
-- Any failed expectation raises an exception and stops psql (use -v ON_ERROR_STOP=1).
begin;

do $verify$
declare
    new_tables constant text[] := array[
        'match_scores', 'competition_countries', 'tickets', 'ticket_fixtures', 'ticket_arm_picks'
    ];
    t text;
    r text;
    stmt text;
    expected text[];
    runtime_oid oid;
begin
    -- 1. Structure and privileges ------------------------------------------------------------
    select oid into runtime_oid from pg_roles where rolname = 'proracle_runtime';
    if runtime_oid is null then
        raise exception 'proracle_runtime role is missing';
    end if;

    foreach t in array new_tables loop
        if not coalesce((select c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
                          where n.nspname = 'proracle' and c.relname = t), false) then
            raise exception 'RLS is not enabled on proracle.%', t;
        end if;
        foreach r in array array['anon', 'authenticated'] loop
            if has_any_column_privilege(r, format('proracle.%I', t), 'SELECT')
               or has_any_column_privilege(r, format('proracle.%I', t), 'INSERT')
               or has_any_column_privilege(r, format('proracle.%I', t), 'UPDATE')
               or has_table_privilege(r, format('proracle.%I', t), 'DELETE')
               or has_table_privilege(r, format('proracle.%I', t), 'TRUNCATE') then
                raise exception 'Unexpected API role access: % on proracle.%', r, t;
            end if;
        end loop;
        foreach r in array array['service_role', 'proracle_runtime'] loop
            if not has_table_privilege(r, format('proracle.%I', t), 'SELECT')
               or not has_table_privilege(r, format('proracle.%I', t), 'INSERT') then
                raise exception '% lacks select or insert on proracle.%', r, t;
            end if;
            -- exact privilege set: only SELECT and INSERT, no column-level extras
            if exists (
                select 1 from pg_class c
                  join pg_namespace n on n.oid = c.relnamespace
                  cross join lateral aclexplode(c.relacl) acl
                 where n.nspname = 'proracle' and c.relname = t
                   and acl.grantee = (select oid from pg_roles where rolname = r)
                   and acl.privilege_type not in ('SELECT', 'INSERT')
            ) or has_any_column_privilege(r, format('proracle.%I', t), 'UPDATE') then
                raise exception '% has more than select and insert on proracle.%', r, t;
            end if;
        end loop;
        if (select count(*) from pg_policies where schemaname = 'proracle' and tablename = t
               and roles = array['proracle_runtime']::name[]) <> 2 then
            raise exception 'proracle.% must have exactly one select and one insert policy for proracle_runtime', t;
        end if;
    end loop;

    -- 2. Synthetic rows written by the owner ---------------------------------------------------
    insert into proracle.fixtures (id, home_team, away_team, kickoff_at, source_name, source_event_key)
    values
        ('00000000-0000-4000-8000-0000000000a1', 'Alpha', 'Beta',  '2030-01-01 18:00:00+00', 'synthetic', 'p0-1'),
        ('00000000-0000-4000-8000-0000000000a2', 'Gamma', 'Delta', '2030-01-01 19:00:00+00', 'synthetic', 'p0-2');
    insert into proracle.predictions (
        id, fixture_id, contract_version, predicted_at, kickoff_at, model_provider, model_name,
        model_version, prompt_version, input_schema_version, input_snapshot_hash,
        home_win_probability, draw_probability, away_win_probability
    ) values (
        '00000000-0000-4000-8000-0000000000b1', '00000000-0000-4000-8000-0000000000a1', '1.0',
        '2030-01-01 12:00:00+00', '2030-01-01 18:00:00+00', 'synthetic', 'test-model', '1',
        'test-prompt-v1', 'features-v1', repeat('a', 64), 0.5, 0.3, 0.2
    );

    -- 3. The runtime role can insert every new table --------------------------------------------
    execute 'set local role proracle_runtime';
    insert into proracle.match_scores (fixture_id, home_score, away_score, source_name)
    values ('00000000-0000-4000-8000-0000000000a1', 2, 1, 'synthetic');
    insert into proracle.competition_countries (competition_id, country, source_name)
    values ('30', 'Africa', 'synthetic');
    insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
    values ('00000000-0000-4000-8000-0000000000c1', '2030-01-01', 'SIMULATED', 'sim-v1', 'seed', 2, 2,
            '2030-01-01 12:00:00+00');
    insert into proracle.ticket_fixtures (ticket_id, fixture_id, position)
    values ('00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a1', 1),
           ('00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a2', 2);
    insert into proracle.ticket_arm_picks (
        ticket_id, fixture_id, arm, arm_version, pick, source_prediction_id, kickoff_at, frozen_at
    ) values (
        '00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a1', 'A',
        'test-model', 'HOME_WIN', '00000000-0000-4000-8000-0000000000b1',
        '2030-01-01 18:00:00+00', '2030-01-01 12:00:00+00'
    );
    insert into proracle.ticket_arm_picks (
        ticket_id, fixture_id, arm, arm_version, pick, forced, score, kickoff_at, frozen_at
    ) values (
        '00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a2', 'C',
        'draw-score-v1', 'DRAW', true, 0.52, '2030-01-01 19:00:00+00', '2030-01-01 12:00:00+00'
    );

    -- 4. Append-only: the runtime role cannot update, delete or truncate ------------------------
    foreach t in array new_tables loop
        foreach stmt in array array[
            format('update proracle.%I set created_at = now()', t),
            format('delete from proracle.%I', t),
            format('truncate proracle.%I', t)
        ] loop
            begin
                execute stmt;
                raise exception 'runtime role was allowed to run: %', stmt;
            exception when insufficient_privilege then
                null;
            end;
        end loop;
    end loop;

    -- 5. Constraints (each statement must fail with the stated error) ---------------------------
    foreach stmt in array array[
        -- a second ticket for the same date and design version
        $s$insert into proracle.tickets (ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
           values ('2030-01-01', 'SIMULATED', 'sim-v1', 'other', 1, 1, '2030-01-01 12:00:00+00')$s$,
        -- a pick frozen at or after kickoff
        $s$insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, forced, score, kickoff_at, frozen_at)
           values ('00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a1', 'C', 'draw-score-v1', 'DRAW', false, 0.4, '2030-01-01 18:00:00+00', '2030-01-01 18:00:00+00')$s$,
        -- the same arm twice on one ticket fixture
        $s$insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, source_prediction_id, kickoff_at, frozen_at)
           values ('00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a1', 'A', 'test-model', 'DRAW', '00000000-0000-4000-8000-0000000000b1', '2030-01-01 18:00:00+00', '2030-01-01 12:00:00+00')$s$,
        -- arm A without its source prediction
        $s$insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, kickoff_at, frozen_at)
           values ('00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a2', 'A', 'test-model', 'DRAW', '2030-01-01 19:00:00+00', '2030-01-01 12:00:00+00')$s$,
        -- forced is only for arm C
        $s$insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, forced, kickoff_at, frozen_at)
           values ('00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a2', 'B', 'rule-1', 'DRAW', true, '2030-01-01 19:00:00+00', '2030-01-01 12:00:00+00')$s$,
        -- an unknown arm
        $s$insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, kickoff_at, frozen_at)
           values ('00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a2', 'D', 'x', 'DRAW', '2030-01-01 19:00:00+00', '2030-01-01 12:00:00+00')$s$,
        -- a pick outside the three outcomes
        $s$insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, kickoff_at, frozen_at)
           values ('00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a2', 'B', 'x', 'WIN', '2030-01-01 19:00:00+00', '2030-01-01 12:00:00+00')$s$,
        -- a score outside 0 to 2
        $s$insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, score, kickoff_at, frozen_at)
           values ('00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a2', 'B', 'x', 'DRAW', 2.5, '2030-01-01 19:00:00+00', '2030-01-01 12:00:00+00')$s$,
        -- a position beyond 20
        $s$insert into proracle.ticket_fixtures (ticket_id, fixture_id, position)
           values ('00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000a1', 21)$s$,
        -- a second row for the same fixture's score
        $s$insert into proracle.match_scores (fixture_id, home_score, away_score, source_name)
           values ('00000000-0000-4000-8000-0000000000a1', 3, 3, 'synthetic')$s$,
        -- a negative score
        $s$insert into proracle.match_scores (fixture_id, home_score, away_score, source_name)
           values ('00000000-0000-4000-8000-0000000000a2', -1, 0, 'synthetic')$s$,
        -- a pick for a fixture that is not on the ticket
        $s$insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, kickoff_at, frozen_at)
           values ('00000000-0000-4000-8000-0000000000c1', '00000000-0000-4000-8000-0000000000ff', 'B', 'x', 'DRAW', '2030-01-01 19:00:00+00', '2030-01-01 12:00:00+00')$s$
    ] loop
        begin
            execute stmt;
            raise exception 'constraint did not fire for: %', left(stmt, 120);
        exception
            when check_violation or unique_violation or foreign_key_violation then
                null;
        end;
    end loop;

    -- 6. Rows read back as the runtime role -------------------------------------------------------
    if (select count(*) from proracle.ticket_arm_picks) <> 2
       or (select count(*) from proracle.ticket_fixtures) <> 2
       or (select count(*) from proracle.match_scores) <> 1 then
        raise exception 'unexpected row counts read back as proracle_runtime';
    end if;

    -- 7. Other roles see nothing ------------------------------------------------------------------
    execute 'reset role';
    foreach r in array array['anon', 'authenticated'] loop
        execute format('set local role %I', r);
        begin
            perform 1 from proracle.tickets;
            raise exception '% could read proracle.tickets', r;
        exception when insufficient_privilege then
            null;
        end;
        execute 'reset role';
    end loop;
end
$verify$;

rollback;
select 'verify_p0_capture: all checks passed' as result;
