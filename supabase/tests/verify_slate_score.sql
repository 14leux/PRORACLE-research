-- Run via psql against a disposable local Supabase database after `supabase db reset --local`.
-- Verifies the slate-score migration (20261130000000): the view's counts (each fixture once however many
-- uploads hold it, only slates, only pre-kickoff predictions, only current final results, the market
-- favourite only where all three prices were printed), that an empty database gives one row of zeros,
-- and the privileges of every role. Rolls back everything it writes. Any failed expectation raises an
-- exception and stops psql (use -v ON_ERROR_STOP=1).
begin;

do $verify$
declare
    f1 constant uuid := '00000000-0000-4000-8000-0000000000a1';  -- settled, priced, model right, market wrong
    f2 constant uuid := '00000000-0000-4000-8000-0000000000a2';  -- settled, unpriced, model wrong
    f3 constant uuid := '00000000-0000-4000-8000-0000000000a3';  -- not settled yet
    f4 constant uuid := '00000000-0000-4000-8000-0000000000a4';  -- on a ticket only: must not count
    f5 constant uuid := '00000000-0000-4000-8000-0000000000a5';  -- no prediction at all (the database forbids one at or after kickoff): must not count
    f6 constant uuid := '00000000-0000-4000-8000-0000000000a6';  -- settled, priced, a result corrected later
    f7 constant uuid := '00000000-0000-4000-8000-0000000000a7';  -- a draw-away tie in the model: the pick is DRAW
    read_a uuid;
    read_b uuid;
    read_t uuid;
    got record;
    res1 uuid;
begin
    -- 1. Structure and privileges ----------------------------------------------------------------------
    if not exists (select 1 from pg_views where schemaname = 'proracle' and viewname = 'intake_slate_score') then
        raise exception 'the view is missing';
    end if;
    if not coalesce((select 'security_invoker=true' = any (c.reloptions) from pg_class c
                      where c.oid = 'proracle.intake_slate_score'::regclass), false) then
        raise exception 'the view must run as the caller';
    end if;
    if not has_table_privilege('service_role', 'proracle.intake_slate_score', 'SELECT') then
        raise exception 'service_role must read the view';
    end if;
    if has_table_privilege('service_role', 'proracle.intake_slate_score', 'INSERT')
       or has_table_privilege('service_role', 'proracle.intake_slate_score', 'UPDATE')
       or has_table_privilege('service_role', 'proracle.intake_slate_score', 'DELETE') then
        raise exception 'service_role must only read the view';
    end if;
    if has_table_privilege('anon', 'proracle.intake_slate_score', 'SELECT')
       or has_table_privilege('authenticated', 'proracle.intake_slate_score', 'SELECT')
       or has_table_privilege('public', 'proracle.intake_slate_score', 'SELECT')
       or has_table_privilege('proracle_runtime', 'proracle.intake_slate_score', 'SELECT') then
        raise exception 'only service_role may read the view';
    end if;

    -- 2. Empty: one row of zeros ---------------------------------------------------------------------------
    execute 'set local role service_role';
    select * into got from proracle.intake_slate_score;
    if (got.slate_reads, got.fixtures, got.settled, got.model_hits, got.compared, got.compared_model_hits, got.compared_market_hits)
       is distinct from (0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint, 0::bigint) then
        raise exception 'an empty database must give one row of zeros, got %', got;
    end if;
    execute 'reset role';

    -- 3. Data -------------------------------------------------------------------------------------------------
    insert into proracle.fixtures (id, home_team, away_team, kickoff_at, source_name, source_event_key) values
        (f1, 'A1', 'B1', now() - interval '5 days', 'synthetic', 'sc-1'),
        (f2, 'A2', 'B2', now() - interval '5 days', 'synthetic', 'sc-2'),
        (f3, 'A3', 'B3', now() + interval '2 days', 'synthetic', 'sc-3'),
        (f4, 'A4', 'B4', now() - interval '5 days', 'synthetic', 'sc-4'),
        (f5, 'A5', 'B5', now() - interval '5 days', 'synthetic', 'sc-5'),
        (f6, 'A6', 'B6', now() - interval '5 days', 'synthetic', 'sc-6'),
        (f7, 'A7', 'B7', now() - interval '5 days', 'synthetic', 'sc-7');
    -- model picks: f1 HOME_WIN, f2 HOME_WIN (a tie of home and draw goes home), f3 AWAY_WIN, f4 DRAW, f5 none, f6 DRAW
    insert into proracle.predictions (
        id, fixture_id, contract_version, predicted_at, kickoff_at, model_provider, model_name,
        model_version, prompt_version, input_schema_version, input_snapshot_hash,
        home_win_probability, draw_probability, away_win_probability
    ) values
        ('00000000-0000-4000-8000-0000000000b1', f1, '1.0', now() - interval '6 days', now() - interval '5 days', 'synthetic', 'elo_davidson_recalibrated', '1', 'p', 'features-v1', repeat('1', 64), 0.5, 0.3, 0.2),
        -- an earlier pick by a different model must not be used
        ('00000000-0000-4000-8000-0000000000b0', f1, '1.0', now() - interval '8 days', now() - interval '5 days', 'synthetic', 'other_model', '1', 'p', 'features-v1', repeat('0', 64), 0.1, 0.1, 0.8),
        -- a later pick by the same model must not replace the earliest
        ('00000000-0000-4000-8000-0000000000b7', f1, '1.0', now() - interval '5 days' - interval '1 hour', now() - interval '5 days', 'synthetic', 'elo_davidson_recalibrated', '2', 'p', 'features-v1', repeat('7', 64), 0.1, 0.2, 0.7),
        ('00000000-0000-4000-8000-0000000000b2', f2, '1.0', now() - interval '6 days', now() - interval '5 days', 'synthetic', 'elo_davidson_recalibrated', '1', 'p', 'features-v1', repeat('2', 64), 0.4, 0.4, 0.2),
        ('00000000-0000-4000-8000-0000000000b3', f3, '1.0', now() - interval '1 day', now() + interval '2 days', 'synthetic', 'elo_davidson_recalibrated', '1', 'p', 'features-v1', repeat('3', 64), 0.2, 0.3, 0.5),
        ('00000000-0000-4000-8000-0000000000b4', f4, '1.0', now() - interval '6 days', now() - interval '5 days', 'synthetic', 'elo_davidson_recalibrated', '1', 'p', 'features-v1', repeat('4', 64), 0.3, 0.4, 0.3),
        ('00000000-0000-4000-8000-0000000000b6', f6, '1.0', now() - interval '6 days', now() - interval '5 days', 'synthetic', 'elo_davidson_recalibrated', '1', 'p', 'features-v1', repeat('6', 64), 0.3, 0.4, 0.3),
        -- f7: draw and away tie for the top: the pick is DRAW (ties go home, then draw, then away)
        ('00000000-0000-4000-8000-0000000000b8', f7, '1.0', now() - interval '6 days', now() - interval '5 days', 'synthetic', 'elo_davidson_recalibrated', '1', 'p', 'features-v1', repeat('8', 64), 0.2, 0.4, 0.4);
    -- results: f1 HOME_WIN, f2 DRAW, f4 DRAW, f5 HOME_WIN, f6 first AWAY_WIN then corrected to DRAW; f3 none
    insert into proracle.results (fixture_id, status, actual_outcome, observed_at, finalized_at, source_name) values
        (f1, 'FINAL', 'HOME_WIN', now() - interval '5 days' + interval '3 hours', now() - interval '5 days' + interval '3 hours', 'synthetic'),
        (f2, 'FINAL', 'DRAW', now() - interval '5 days' + interval '3 hours', now() - interval '5 days' + interval '3 hours', 'synthetic'),
        (f4, 'FINAL', 'DRAW', now() - interval '5 days' + interval '3 hours', now() - interval '5 days' + interval '3 hours', 'synthetic'),
        (f5, 'FINAL', 'HOME_WIN', now() - interval '5 days' + interval '3 hours', now() - interval '5 days' + interval '3 hours', 'synthetic'),
        (f7, 'FINAL', 'DRAW', now() - interval '5 days' + interval '3 hours', now() - interval '5 days' + interval '3 hours', 'synthetic');
    insert into proracle.results (fixture_id, status, actual_outcome, observed_at, finalized_at, source_name)
    values (f6, 'FINAL', 'AWAY_WIN', now() - interval '5 days' + interval '3 hours', now() - interval '5 days' + interval '3 hours', 'synthetic')
    returning id into res1;
    insert into proracle.results (fixture_id, status, actual_outcome, observed_at, finalized_at, source_name, supersedes_result_id)
    values (f6, 'FINAL', 'DRAW', now() - interval '4 days', now() - interval '4 days', 'synthetic', res1);

    insert into proracle.intake_updates (update_id, kind) values (910000001, 'PDF'), (910000002, 'PDF'), (910000003, 'PDF');
    execute 'set local role service_role';
    -- two slate uploads (a repeat of f1 and f2 across them), and one ticket that holds f4
    insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched, kind, received_at)
    values (910000001, current_date, 'PDF', 'm', 4, 4, 4, 'SLATE', now() - interval '3 days') returning id into read_a;
    insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched, kind, received_at)
    values (910000002, current_date, 'PDF', 'm', 4, 4, 4, 'SLATE', now() - interval '2 days') returning id into read_b;
    insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched)
    values (910000003, current_date, 'PDF', 'm', 1, 1, 1) returning id into read_t;
    -- f1: first upload has no prices, the second prints all three: the second's prices are kept. Market favourite
    -- is the AWAY side (lowest odds), so the market is wrong where the model is right.
    insert into proracle.intake_ticket_rows (intake_id, read_kind, position, home_team, away_team, home_odds, draw_odds, away_odds, matched_fixture_id) values
        (read_a, 'SLATE', 1, 'A1', 'B1', null, null, null, f1),
        (read_a, 'SLATE', 2, 'A2', 'B2', null, null, null, f2),
        (read_a, 'SLATE', 3, 'A3', 'B3', null, null, null, f3),
        (read_a, 'SLATE', 4, 'A5', 'B5', null, null, null, f5),
        (read_a, 'SLATE', 5, 'A7', 'B7', null, null, null, f7),
        (read_b, 'SLATE', 1, 'A1', 'B1', 3.40, 3.30, 2.10, f1),
        (read_b, 'SLATE', 2, 'A2', 'B2', null, null, null, f2),
        -- f6 priced: market favourite is the DRAW (lowest odds), right after the correction; model also DRAW
        (read_b, 'SLATE', 3, 'A6', 'B6', 3.00, 2.00, 4.00, f6),
        -- an unmatched row never counts
        (read_b, 'SLATE', 4, 'Nobody', 'Nowhere', 2.0, 3.0, 4.0, null);
    insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, home_odds, draw_odds, away_odds, matched_fixture_id)
    values (read_t, 1, 'A4', 'B4', 2.0, 3.0, 4.0, f4);

    select * into got from proracle.intake_slate_score;
    -- slates: 2 reads. fixtures: f1, f2, f3, f5, f6, f7 = 6 (f4 is on a ticket only).
    -- (settled also includes f7: model DRAW on a draw-away tie, actual DRAW, right)
    -- settled: f1 (model HOME_WIN, right), f2 (HOME_WIN tie, actual DRAW, wrong), f6 (DRAW, right after correction);
    --   f3 has no result; f5 has no pre-kickoff prediction.
    -- compared (all three prices): f1 (market AWAY_WIN, wrong), f6 (market DRAW, right). f2 has no prices.
    if (got.slate_reads, got.fixtures, got.settled, got.model_hits, got.compared, got.compared_model_hits, got.compared_market_hits)
       is distinct from (2::bigint, 6::bigint, 4::bigint, 3::bigint, 2::bigint, 2::bigint, 1::bigint) then
        raise exception 'slate score counts wrong, got %', got;
    end if;
    -- the view is read-only
    begin
        insert into proracle.intake_slate_score (slate_reads) values (1);
        raise exception 'the view accepted an insert';
    exception when insufficient_privilege or feature_not_supported or object_not_in_prerequisite_state then null;
    end;
    execute 'reset role';

    -- 4. A role without the grant sees nothing of it --------------------------------------------------------
    execute 'set local role proracle_runtime';
    begin
        perform 1 from proracle.intake_slate_score;
        raise exception 'the runtime role read the view';
    exception when insufficient_privilege then null;
    end;
    execute 'reset role';
end
$verify$;

rollback;
select 'verify_slate_score: all checks passed' as result;
