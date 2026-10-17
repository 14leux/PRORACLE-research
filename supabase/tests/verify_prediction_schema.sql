-- Run via psql against a fresh, disposable local Supabase database.
-- The transaction rolls back every fixture, prediction, result, and failure row.
begin;

-- Model pre-existing legacy tables that use the same generic names and verify that PRORACLE's
-- dedicated namespace can coexist without taking ownership of them.
create table public.fixtures (id integer primary key, legacy_marker text not null);
create table public.predictions (id integer primary key, legacy_marker text not null);
create table public.results (id integer primary key, legacy_marker text not null);
insert into public.fixtures values (1, 'LEGACY_FIXTURES_PRESERVED');
insert into public.predictions values (1, 'LEGACY_PREDICTIONS_PRESERVED');
insert into public.results values (1, 'LEGACY_RESULTS_PRESERVED');

do $privilege_checks$
declare
    table_name text;
    role_name text;
    column_name text;
    service_role_oid oid;
begin
    if not has_schema_privilege('service_role', 'proracle', 'USAGE')
       or has_schema_privilege('anon', 'proracle', 'USAGE')
       or has_schema_privilege('authenticated', 'proracle', 'USAGE') then
        raise exception 'PRORACLE schema usage grants do not match the intended runtime boundary';
    end if;

    select oid into service_role_oid from pg_roles where rolname = 'service_role';
    if service_role_oid is null then
        raise exception 'service_role is missing from this Supabase database';
    end if;

    foreach table_name in array array['fixtures', 'predictions', 'results', 'prediction_failures'] loop
        if not coalesce((select c.relrowsecurity
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
                where n.nspname = 'proracle' and c.relname = table_name), false) then
            raise exception 'RLS is not enabled on proracle.%', table_name;
        end if;

        foreach role_name in array array['anon', 'authenticated'] loop
            if has_table_privilege(role_name, format('proracle.%I', table_name), 'SELECT')
               or has_table_privilege(role_name, format('proracle.%I', table_name), 'INSERT')
               or has_table_privilege(role_name, format('proracle.%I', table_name), 'UPDATE')
               or has_table_privilege(role_name, format('proracle.%I', table_name), 'DELETE')
               or has_table_privilege(role_name, format('proracle.%I', table_name), 'TRUNCATE')
               or has_table_privilege(role_name, format('proracle.%I', table_name), 'REFERENCES')
               or has_table_privilege(role_name, format('proracle.%I', table_name), 'TRIGGER')
               or has_any_column_privilege(role_name, format('proracle.%I', table_name), 'SELECT')
               or has_any_column_privilege(role_name, format('proracle.%I', table_name), 'INSERT')
               or has_any_column_privilege(role_name, format('proracle.%I', table_name), 'UPDATE') then
                raise exception 'Unexpected API role access: % on proracle.%', role_name, table_name;
            end if;
        end loop;

        if exists (
            select 1
              from pg_class c
              join pg_namespace n on n.oid = c.relnamespace
              cross join lateral aclexplode(c.relacl) acl
             where n.nspname = 'proracle'
               and c.relname = table_name
               and acl.grantee = service_role_oid
               and acl.privilege_type not in ('SELECT', 'INSERT')
        ) then
            raise exception 'Unexpected direct table ACL for service_role on proracle.%', table_name;
        end if;
    end loop;

    if not has_table_privilege('service_role', 'proracle.fixtures', 'SELECT')
       or not has_table_privilege('service_role', 'proracle.fixtures', 'INSERT')
       or not has_column_privilege('service_role', 'proracle.fixtures', 'status', 'UPDATE')
       or not has_column_privilege('service_role', 'proracle.fixtures', 'kickoff_at', 'UPDATE')
       or has_column_privilege('service_role', 'proracle.fixtures', 'home_team', 'UPDATE')
       or has_table_privilege('service_role', 'proracle.fixtures', 'DELETE')
       or has_table_privilege('service_role', 'proracle.fixtures', 'TRUNCATE')
       or has_table_privilege('service_role', 'proracle.fixtures', 'TRIGGER')
       or has_table_privilege('service_role', 'proracle.fixtures', 'REFERENCES') then
        raise exception 'Fixture grants do not match the intended runtime permissions';
    end if;

    foreach column_name in array array[
        'id', 'sport', 'competition_id', 'competition_name', 'home_team',
        'away_team', 'source_name', 'source_event_key', 'created_at'
    ] loop
        if has_column_privilege('service_role', 'proracle.fixtures', column_name, 'UPDATE') then
            raise exception 'service_role can unexpectedly update fixtures.%', column_name;
        end if;
    end loop;

    foreach table_name in array array['predictions', 'results', 'prediction_failures'] loop
        if not has_table_privilege('service_role', format('proracle.%I', table_name), 'SELECT')
           or not has_table_privilege('service_role', format('proracle.%I', table_name), 'INSERT')
           or has_table_privilege('service_role', format('proracle.%I', table_name), 'UPDATE')
           or has_table_privilege('service_role', format('proracle.%I', table_name), 'DELETE')
           or has_table_privilege('service_role', format('proracle.%I', table_name), 'TRUNCATE')
           or has_table_privilege('service_role', format('proracle.%I', table_name), 'TRIGGER')
           or has_table_privilege('service_role', format('proracle.%I', table_name), 'REFERENCES')
           or has_any_column_privilege('service_role', format('proracle.%I', table_name), 'UPDATE') then
            raise exception 'Append-only runtime grants are too broad for proracle.%', table_name;
        end if;
    end loop;

    if not has_table_privilege('service_role', 'proracle.prediction_failures', 'SELECT')
       or not has_table_privilege('service_role', 'proracle.prediction_failures', 'INSERT') then
        raise exception 'service_role cannot read and append safe prediction failures';
    end if;

    if current_setting('server_version_num')::integer >= 170000
       and (
           has_table_privilege('service_role', 'proracle.fixtures', 'MAINTAIN')
           or has_table_privilege('service_role', 'proracle.predictions', 'MAINTAIN')
           or has_table_privilege('service_role', 'proracle.results', 'MAINTAIN')
           or has_table_privilege('service_role', 'proracle.prediction_failures', 'MAINTAIN')
       ) then
        raise exception 'service_role unexpectedly has MAINTAIN';
    end if;
end;
$privilege_checks$;

do $legacy_collision_checks$
begin
    if (select legacy_marker from public.fixtures where id = 1) <> 'LEGACY_FIXTURES_PRESERVED'
       or (select legacy_marker from public.predictions where id = 1) <> 'LEGACY_PREDICTIONS_PRESERVED'
       or (select legacy_marker from public.results where id = 1) <> 'LEGACY_RESULTS_PRESERVED' then
        raise exception 'PRORACLE namespace conflicts with the existing public table names';
    end if;
end;
$legacy_collision_checks$;

set local role service_role;

insert into proracle.fixtures (
    id, home_team, away_team, kickoff_at, source_name, source_event_key
) values (
    '00000000-0000-4000-8000-000000000101', 'Synthetic Home', 'Synthetic Away',
    '2030-01-01 18:00:00+00', 'synthetic', 'fixture-101'
);

do $source_key_idempotency_check$
begin
    begin
        insert into proracle.fixtures (
            id, home_team, away_team, kickoff_at, source_name, source_event_key
        ) values (
            '00000000-0000-4000-8000-000000000109', 'Duplicate Home', 'Duplicate Away',
            '2030-01-01 18:00:00+00', 'synthetic', 'fixture-101'
        );
        raise exception using errcode = 'P0001', message = 'Duplicate source event key unexpectedly passed';
    exception when unique_violation then
        null;
    end;
end;
$source_key_idempotency_check$;

insert into proracle.predictions (
    id, fixture_id, contract_version, predicted_at, kickoff_at,
    model_provider, model_name, model_version, prompt_version,
    input_schema_version, input_snapshot_hash,
    home_win_probability, draw_probability, away_win_probability
) values (
    '00000000-0000-4000-8000-000000000102',
    '00000000-0000-4000-8000-000000000101', '1.0',
    '2030-01-01 12:00:00+00', '2030-01-01 18:00:00+00',
    'synthetic', 'test-model', '1', 'test-prompt-v1', 'features-v1', repeat('a', 64),
    0.48, 0.27, 0.25
);

insert into proracle.results (
    id, fixture_id, status, actual_outcome, observed_at, finalized_at, source_name
) values (
    '00000000-0000-4000-8000-000000000103',
    '00000000-0000-4000-8000-000000000101', 'FINAL', 'HOME_WIN',
    '2030-01-01 20:00:00+00', '2030-01-01 20:00:00+00', 'synthetic'
);

insert into proracle.prediction_failures (
    event_id, fixture_id, contract_version, occurred_at, kickoff_at,
    stage, error_code, retryable
) values (
    '00000000-0000-4000-8000-000000000110',
    '00000000-0000-4000-8000-000000000101', '1.0',
    '2030-01-01 12:10:00+00', '2030-01-01 18:00:00+00',
    'SOURCE_REQUEST', 'SOURCE_TIMEOUT', true
);

insert into proracle.results (
    id, fixture_id, supersedes_result_id, status, actual_outcome,
    observed_at, finalized_at, source_name
) values (
    '00000000-0000-4000-8000-000000000104',
    '00000000-0000-4000-8000-000000000101',
    '00000000-0000-4000-8000-000000000103', 'FINAL', 'DRAW',
    '2030-01-02 08:00:00+00', '2030-01-01 20:00:00+00', 'synthetic-correction'
);

do $constraint_checks$
begin
    begin
        insert into proracle.predictions (
            id, fixture_id, contract_version, predicted_at, kickoff_at,
            model_provider, model_name, model_version, prompt_version,
            input_schema_version, input_snapshot_hash,
            home_win_probability, draw_probability, away_win_probability
        ) values (
            '00000000-0000-4000-8000-000000000105',
            '00000000-0000-4000-8000-000000000101', '1.0',
            '2030-01-01 12:00:00+00', '2030-01-01 18:00:00+00',
            'synthetic', 'test-model', '1', 'test-prompt-v1', 'features-v1', repeat('b', 64),
            0.20, 0.20, 0.20
        );
        raise exception using errcode = 'P0001', message = 'Invalid probability sum unexpectedly passed';
    exception when check_violation then
        null;
    end;

    begin
        insert into proracle.predictions (
            id, fixture_id, contract_version, predicted_at, kickoff_at,
            model_provider, model_name, model_version, prompt_version,
            input_schema_version, input_snapshot_hash,
            home_win_probability, draw_probability, away_win_probability
        ) values (
            '00000000-0000-4000-8000-000000000108',
            '00000000-0000-4000-8000-000000000101', '1.0',
            '2030-01-01 18:00:00+00', '2030-01-01 18:00:00+00',
            'synthetic', 'test-model', '1', 'test-prompt-v1', 'features-v1', repeat('c', 64),
            0.48, 0.27, 0.25
        );
        raise exception using errcode = 'P0001', message = 'Prediction at kickoff unexpectedly passed';
    exception when check_violation then
        null;
    end;

    begin
        insert into proracle.results (
            id, fixture_id, status, actual_outcome, observed_at, finalized_at, source_name
        ) values (
            '00000000-0000-4000-8000-000000000106',
            '00000000-0000-4000-8000-000000000101', 'FINAL', null,
            '2030-01-01 20:00:00+00', '2030-01-01 20:00:00+00', 'synthetic'
        );
        raise exception using errcode = 'P0001', message = 'Final result without outcome unexpectedly passed';
    exception when check_violation then
        null;
    end;

    begin
        insert into proracle.results (
            id, fixture_id, supersedes_result_id, status, actual_outcome,
            observed_at, finalized_at, source_name
        ) values (
            '00000000-0000-4000-8000-000000000107',
            '00000000-0000-4000-8000-000000000101',
            '00000000-0000-4000-8000-000000000103', 'FINAL', 'AWAY_WIN',
            '2030-01-03 08:00:00+00', '2030-01-01 20:00:00+00', 'synthetic-second-correction'
        );
        raise exception using errcode = 'P0001', message = 'Second correction for same result unexpectedly passed';
    exception when unique_violation then
        null;
    end;

    begin
        insert into proracle.prediction_failures (
            event_id, fixture_id, contract_version, occurred_at, kickoff_at,
            stage, error_code, retryable
        ) values (
            '00000000-0000-4000-8000-000000000111',
            '00000000-0000-4000-8000-000000000101', '1.0',
            '2030-01-01 12:10:00+00', '2030-01-01 18:00:00+00',
            'INPUT_VALIDATION', 'SOURCE_TIMEOUT', true
        );
        raise exception using errcode = 'P0001', message = 'Failure code for wrong stage unexpectedly passed';
    exception when check_violation then
        null;
    end;

    begin
        insert into proracle.prediction_failures (
            event_id, fixture_id, contract_version, occurred_at, kickoff_at,
            stage, error_code, retryable
        ) values (
            '00000000-0000-4000-8000-000000000112',
            '00000000-0000-4000-8000-000000000101', '1.0',
            '2030-01-01 18:00:00+00', '2030-01-01 18:00:00+00',
            'SOURCE_REQUEST', 'SOURCE_TIMEOUT', true
        );
        raise exception using errcode = 'P0001', message = 'Post-kickoff failure unexpectedly passed';
    exception when check_violation then
        null;
    end;
end;
$constraint_checks$;

do $service_role_checks$
begin
    if (select count(*) from proracle.fixtures
        where id = '00000000-0000-4000-8000-000000000101') <> 1 then
        raise exception 'service_role could not read the inserted fixture';
    end if;

    if (select count(*) from proracle.predictions
        where id = '00000000-0000-4000-8000-000000000102') <> 1 then
        raise exception 'service_role could not read the inserted prediction';
    end if;

    if (select count(*) from proracle.results
        where fixture_id = '00000000-0000-4000-8000-000000000101') <> 2 then
        raise exception 'service_role could not read the result versions';
    end if;

    if (select count(*) from proracle.prediction_failures
        where event_id = '00000000-0000-4000-8000-000000000110') <> 1 then
        raise exception 'service_role could not read the safe failure event';
    end if;

    update proracle.fixtures
       set status = 'POSTPONED', kickoff_at = '2030-01-02 18:00:00+00'
     where id = '00000000-0000-4000-8000-000000000101';

    begin
        update proracle.fixtures set home_team = 'Changed'
         where id = '00000000-0000-4000-8000-000000000101';
        raise exception using errcode = 'P0001', message = 'Unexpected fixture team update permission';
    exception when insufficient_privilege then
        null;
    end;

    begin
        update proracle.predictions set home_win_probability = 0.50
         where id = '00000000-0000-4000-8000-000000000102';
        raise exception using errcode = 'P0001', message = 'Unexpected prediction update permission';
    exception when insufficient_privilege then
        null;
    end;

    begin
        delete from proracle.predictions
         where id = '00000000-0000-4000-8000-000000000102';
        raise exception using errcode = 'P0001', message = 'Unexpected prediction delete permission';
    exception when insufficient_privilege then
        null;
    end;

    begin
        update proracle.results set actual_outcome = 'AWAY_WIN'
         where id = '00000000-0000-4000-8000-000000000104';
        raise exception using errcode = 'P0001', message = 'Unexpected result update permission';
    exception when insufficient_privilege then
        null;
    end;

    begin
        delete from proracle.results
         where id = '00000000-0000-4000-8000-000000000104';
        raise exception using errcode = 'P0001', message = 'Unexpected result delete permission';
    exception when insufficient_privilege then
        null;
    end;

    begin
        update proracle.prediction_failures set error_code = 'SOURCE_UNAVAILABLE'
         where event_id = '00000000-0000-4000-8000-000000000110';
        raise exception using errcode = 'P0001', message = 'Unexpected failure event update permission';
    exception when insufficient_privilege then
        null;
    end;
end;
$service_role_checks$;

reset role;
set local role anon;

do $anon_denial_checks$
begin
    begin
        execute 'select id from proracle.fixtures limit 1';
        raise exception using errcode = 'P0001', message = 'anon unexpectedly read fixtures';
    exception when insufficient_privilege then
        null;
    end;

    begin
        execute 'select event_id from proracle.prediction_failures limit 1';
        raise exception using errcode = 'P0001', message = 'anon unexpectedly read prediction failures';
    exception when insufficient_privilege then
        null;
    end;

    begin
        execute $sql$insert into proracle.fixtures (home_team, away_team, kickoff_at)
                 values ('Anon', 'Write', '2030-01-01 18:00:00+00')$sql$;
        raise exception using errcode = 'P0001', message = 'anon unexpectedly inserted a fixture';
    exception when insufficient_privilege then
        null;
    end;
end;
$anon_denial_checks$;

reset role;
set local role authenticated;

do $authenticated_denial_checks$
begin
    begin
        execute 'select id from proracle.predictions limit 1';
        raise exception using errcode = 'P0001', message = 'authenticated unexpectedly read predictions';
    exception when insufficient_privilege then
        null;
    end;

    begin
        execute $sql$insert into proracle.predictions (
                     id, fixture_id, contract_version, predicted_at, kickoff_at,
                     model_provider, model_name, model_version, prompt_version,
                     input_schema_version, input_snapshot_hash,
                     home_win_probability, draw_probability, away_win_probability
                 ) values (
                     '00000000-0000-4000-8000-000000000105',
                     '00000000-0000-4000-8000-000000000101', '1.0',
                     '2030-01-01 12:00:00+00', '2030-01-01 18:00:00+00',
                     'synthetic', 'test-model', '1', 'test-prompt-v1', 'features-v1', repeat('b', 64),
                     0.48, 0.27, 0.25
                 )$sql$;
        raise exception using errcode = 'P0001', message = 'authenticated unexpectedly inserted a prediction';
    exception when insufficient_privilege then
        null;
    end;
end;
$authenticated_denial_checks$;

rollback;
