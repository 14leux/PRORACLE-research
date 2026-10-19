-- Least-privilege runtime role for PRORACLE (see docs/RESTRICTED_ROLE.md).
-- A JWT whose `role` claim is `proracle_runtime` makes PostgREST switch to this role, which can
-- act on the proracle schema only. Revoke it instantly with:  revoke proracle_runtime from authenticator;
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'proracle_runtime') then
    create role proracle_runtime nologin noinherit nobypassrls;
  end if;
end
$$;

grant proracle_runtime to authenticator;
grant usage on schema proracle to proracle_runtime;

grant select, insert on table proracle.fixtures to proracle_runtime;
grant update (kickoff_at, status) on table proracle.fixtures to proracle_runtime;
grant select, insert on table proracle.predictions to proracle_runtime;
grant select, insert on table proracle.results to proracle_runtime;
grant select, insert on table proracle.prediction_failures to proracle_runtime;

-- Row level security stays on. service_role bypasses it; a custom role does not, so it needs policies.
create policy proracle_runtime_select_fixtures on proracle.fixtures for select to proracle_runtime using (true);
create policy proracle_runtime_insert_fixtures on proracle.fixtures for insert to proracle_runtime with check (true);
create policy proracle_runtime_update_fixtures on proracle.fixtures for update to proracle_runtime using (true) with check (true);
create policy proracle_runtime_select_predictions on proracle.predictions for select to proracle_runtime using (true);
create policy proracle_runtime_insert_predictions on proracle.predictions for insert to proracle_runtime with check (true);
create policy proracle_runtime_select_results on proracle.results for select to proracle_runtime using (true);
create policy proracle_runtime_insert_results on proracle.results for insert to proracle_runtime with check (true);
create policy proracle_runtime_select_failures on proracle.prediction_failures for select to proracle_runtime using (true);
create policy proracle_runtime_insert_failures on proracle.prediction_failures for insert to proracle_runtime with check (true);
