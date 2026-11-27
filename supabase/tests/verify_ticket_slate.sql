-- Run via psql against a disposable local Supabase database after `supabase db reset --local`.
-- Verifies the tickets-and-slates migration (20261129000000): the ticket 20-row cap against the slate
-- 100-row limit, match reasons, rejected rows, the pending upload and its expiry, file hashes, tokens,
-- ticket kickoff and review times, the send-once review record, the recent-window view, the new audit
-- actions, that what the deployed function and the workflows already read still works, and the
-- privileges of every role. Rolls back everything it writes. Any failed expectation raises an
-- exception and stops psql (use -v ON_ERROR_STOP=1).
begin;

do $verify$
declare
    new_tables constant text[] := array[
        'intake_rejected_rows', 'intake_pending_uploads', 'intake_file_hashes', 'ticket_reviews'
    ];
    new_views constant text[] := array['intake_pending_upload', 'intake_recent_window'];
    t text;
    r text;
    n bigint;
    f1 constant uuid := '00000000-0000-4000-8000-0000000000f1';
    f2 constant uuid := '00000000-0000-4000-8000-0000000000f2';
    f3 constant uuid := '00000000-0000-4000-8000-0000000000f3';
    pred1 constant uuid := '00000000-0000-4000-8000-0000000000c1';
    pred2 constant uuid := '00000000-0000-4000-8000-0000000000c2';
    tk constant uuid := '00000000-0000-4000-8000-0000000000d1';
    tk_old constant uuid := '00000000-0000-4000-8000-0000000000d2';
    read_ticket uuid;
    read_slate uuid;
    read_old uuid;
    pend1 uuid;
    pend2 uuid;
    pend3 uuid;
    got record;
begin
    -- 1. Structure and privileges --------------------------------------------------------------------
    foreach t in array new_tables loop
        if not coalesce((select c.relrowsecurity from pg_class c join pg_namespace n on n.oid = c.relnamespace
                          where n.nspname = 'proracle' and c.relname = t), false) then
            raise exception 'RLS is not enabled on proracle.%', t;
        end if;
    end loop;
    foreach t in array new_tables || new_views loop
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
           or has_table_privilege('service_role', format('proracle.%I', t), 'TRUNCATE')
           or has_any_column_privilege('service_role', format('proracle.%I', t), 'UPDATE')
           or has_table_privilege('proracle_runtime', format('proracle.%I', t), 'DELETE')
           or has_table_privilege('proracle_runtime', format('proracle.%I', t), 'TRUNCATE')
           or has_any_column_privilege('proracle_runtime', format('proracle.%I', t), 'UPDATE') then
            raise exception 'a role can update, delete or truncate proracle.%', t;
        end if;
    end loop;
    -- the function (service_role) reads and appends rejected rows, pending uploads and file hashes
    foreach t in array array['intake_rejected_rows', 'intake_pending_uploads', 'intake_file_hashes'] loop
        if not has_table_privilege('service_role', format('proracle.%I', t), 'SELECT')
           or not has_table_privilege('service_role', format('proracle.%I', t), 'INSERT') then
            raise exception 'service_role must select and insert on proracle.%', t;
        end if;
    end loop;
    -- reviews: written by the workflow (runtime), only read by the function
    if has_table_privilege('service_role', 'proracle.ticket_reviews', 'INSERT')
       or not has_table_privilege('service_role', 'proracle.ticket_reviews', 'SELECT')
       or not has_table_privilege('proracle_runtime', 'proracle.ticket_reviews', 'SELECT')
       or not has_table_privilege('proracle_runtime', 'proracle.ticket_reviews', 'INSERT') then
        raise exception 'ticket_reviews privileges are not: service_role select; runtime select and insert';
    end if;
    -- the runtime role reads rejected rows only; it never touches pending uploads or file hashes
    if not has_table_privilege('proracle_runtime', 'proracle.intake_rejected_rows', 'SELECT')
       or has_table_privilege('proracle_runtime', 'proracle.intake_rejected_rows', 'INSERT')
       or has_any_column_privilege('proracle_runtime', 'proracle.intake_pending_uploads', 'SELECT')
       or has_any_column_privilege('proracle_runtime', 'proracle.intake_pending_uploads', 'INSERT')
       or has_any_column_privilege('proracle_runtime', 'proracle.intake_file_hashes', 'SELECT')
       or has_any_column_privilege('proracle_runtime', 'proracle.intake_file_hashes', 'INSERT')
       or has_table_privilege('proracle_runtime', 'proracle.intake_pending_upload', 'SELECT')
       or has_table_privilege('proracle_runtime', 'proracle.intake_recent_window', 'SELECT') then
        raise exception 'the runtime role has access to something it does not need';
    end if;
    foreach t in array new_views loop
        if not has_table_privilege('service_role', format('proracle.%I', t), 'SELECT') then
            raise exception 'service_role cannot read proracle.%', t;
        end if;
    end loop;
    -- the daily token totals: service_role may change only them and updated_at, never the date
    if not has_column_privilege('service_role', 'proracle.intake_usage', 'input_tokens', 'UPDATE')
       or not has_column_privilege('service_role', 'proracle.intake_usage', 'output_tokens', 'UPDATE')
       or not has_column_privilege('service_role', 'proracle.intake_usage', 'vision_calls', 'UPDATE')
       or has_column_privilege('service_role', 'proracle.intake_usage', 'usage_date', 'UPDATE') then
        raise exception 'service_role update rights on intake_usage are wrong';
    end if;
    if not has_function_privilege('service_role', 'proracle.intake_add_tokens(date, bigint, bigint)', 'EXECUTE')
       or has_function_privilege('anon', 'proracle.intake_add_tokens(date, bigint, bigint)', 'EXECUTE')
       or has_function_privilege('authenticated', 'proracle.intake_add_tokens(date, bigint, bigint)', 'EXECUTE')
       or has_function_privilege('proracle_runtime', 'proracle.intake_add_tokens(date, bigint, bigint)', 'EXECUTE') then
        raise exception 'intake_add_tokens must be executable by service_role only';
    end if;
    -- the older tables and views keep their earlier privileges (the migration revoked nothing there)
    if not has_table_privilege('service_role', 'proracle.intake_tickets', 'INSERT')
       or has_any_column_privilege('service_role', 'proracle.intake_tickets', 'UPDATE')
       or has_table_privilege('proracle_runtime', 'proracle.intake_tickets', 'INSERT')
       or not has_table_privilege('proracle_runtime', 'proracle.intake_ticket_rows', 'SELECT')
       or not has_table_privilege('proracle_runtime', 'proracle.tickets', 'INSERT')
       or not has_table_privilege('service_role', 'proracle.intake_scoreboard', 'SELECT')
       or not has_table_privilege('proracle_runtime', 'proracle.intake_active_rules', 'SELECT') then
        raise exception 'an earlier privilege changed';
    end if;

    -- 2. What the deployed function and the workflows already do still works (no new columns used) ------
    insert into proracle.fixtures (id, home_team, away_team, kickoff_at, source_name, source_event_key) values
        (f1, 'Alpha', 'Beta', now() - interval '5 days', 'synthetic', 'ts-1'),
        (f2, 'Gamma', 'Delta', now() - interval '40 days', 'synthetic', 'ts-2'),
        (f3, 'Eps', 'Zeta', now() + interval '2 days', 'synthetic', 'ts-3');
    insert into proracle.intake_updates (update_id, kind) values
        (900000001, 'PDF'), (900000002, 'IMAGE'), (900000003, 'COMMAND'), (900000004, 'COMMAND'),
        (900000005, 'COMMAND'), (900000006, 'COMMAND'), (900000007, 'PDF'), (900000008, 'PDF');
    execute 'set local role service_role';
    -- exactly what the function writes today: no kind, tokens, read_kind or match columns
    insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched)
    values (900000001, current_date, 'PDF', 'claude-sonnet-5-5', 25, 20, 1)
    returning id into read_old;
    insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, kickoff_at, home_odds, draw_odds, away_odds, matched_fixture_id)
    values (read_old, 1, 'Alpha', 'Beta', now() - interval '5 days', 2.1, 3.3, 3.6, f1),
           (read_old, 20, 'Gamma', 'Delta', now() - interval '40 days', null, null, null, null);
    select count(*) into n from proracle.intake_ticket_rows where intake_id = read_old and kickoff_at is not null;
    if n <> 2 then raise exception 'the older insert shape no longer works'; end if;
    select kind into got from proracle.intake_tickets where id = read_old;
    if got.kind <> 'TICKET' then raise exception 'an older read must default to TICKET'; end if;
    if proracle.intake_reserve_call(date '2031-01-15', 5) is not true then raise exception 'intake_reserve_call broke'; end if;
    perform 1 from proracle.intake_active_rule;
    perform 1 from proracle.intake_active_rules;
    perform 1 from proracle.intake_pending_rule;
    perform 1 from proracle.intake_pending_retirement;
    perform 1 from proracle.intake_arm_scoreboard;
    perform 1 from proracle.intake_scoreboard;
    insert into proracle.intake_audit (action, outcome, detail) values ('TICKET', 'OK', 'read=25 kept=20 matched=1');
    execute 'reset role';
    -- the workflow's reads, as the restricted runtime role, with the columns the adapter selects
    execute 'set local role proracle_runtime';
    perform id, ticket_date, bookmaker_label, rows_kept, rows_matched from proracle.intake_tickets where id = read_old;
    select count(*) into n
      from proracle.intake_ticket_rows r join proracle.fixtures f on f.id = r.matched_fixture_id
     where r.intake_id = read_old;
    if n <> 1 then raise exception 'the runtime role cannot read the rows the way the adapter does'; end if;
    perform 1 from proracle.intake_active_rules;
    perform last_kickoff_at, review_due_at from proracle.tickets;
    execute 'reset role';

    -- 3. Ticket against slate limits ------------------------------------------------------------------
    begin
        insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched)
        values (900000002, current_date, 'IMAGE', 'm', 40, 21, 0);
        raise exception 'a ticket of 21 rows was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched, kind)
        values (900000002, current_date, 'IMAGE', 'm', 150, 101, 0, 'SLATE');
        raise exception 'a slate of 101 rows was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched, kind)
        values (900000002, current_date, 'IMAGE', 'm', 5, 5, 0, 'RANDOM');
        raise exception 'an unknown kind was accepted';
    exception when check_violation then null;
    end;
    insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched, kind, input_tokens, output_tokens)
    values (900000002, current_date, 'IMAGE', 'm', 150, 100, 60, 'SLATE', 4200, 1800)
    returning id into read_slate;
    insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched, input_tokens, output_tokens)
    values (900000007, current_date, 'PDF', 'm', 20, 20, 20, 3000, 900)
    returning id into read_ticket;
    begin
        insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched, input_tokens)
        values (900000008, current_date, 'PDF', 'm', 1, 1, 0, -1);
        raise exception 'a negative token count was accepted';
    exception when check_violation then null;
    end;
    -- rows: positions up to 100 for a slate, 20 for a ticket, kind taken from the parent
    insert into proracle.intake_ticket_rows (intake_id, read_kind, position, home_team, away_team)
    values (read_slate, 'SLATE', 100, 'S100h', 'S100a');
    begin
        insert into proracle.intake_ticket_rows (intake_id, read_kind, position, home_team, away_team)
        values (read_slate, 'SLATE', 101, 'x', 'y');
        raise exception 'row position 101 was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team)
        values (read_ticket, 21, 'x', 'y');
        raise exception 'a ticket row at position 21 was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_ticket_rows (intake_id, read_kind, position, home_team, away_team)
        values (read_ticket, 'SLATE', 21, 'x', 'y');
        raise exception 'a slate-kind row under a ticket was accepted';
    exception when foreign_key_violation then null;
    end;
    begin
        insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team)
        values (read_slate, 5, 'x', 'y');
        raise exception 'a ticket-kind row under a slate was accepted';
    exception when foreign_key_violation then null;
    end;
    begin
        insert into proracle.tickets (ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
        values ('2031-01-01', 'SIMULATED', 'v', 's', 21, 21, now());
        raise exception 'a stored ticket of 21 fixtures was accepted';
    exception when check_violation then null;
    end;

    -- 4. Match reasons --------------------------------------------------------------------------------
    insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, kickoff_at, matched_fixture_id, match_via)
    values (read_ticket, 1, 'Alpha', 'Beta', now() - interval '5 days', f1, 'ANCHOR');
    insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, unmatched_reason)
    values (read_ticket, 2, 'Nobody', 'Here', 'NO_KICKOFF'),
           (read_ticket, 3, 'Some', 'Where', 'NO_MATCH'),
           (read_ticket, 4, 'Two', 'Candidates', 'AMBIGUOUS');
    begin
        insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, matched_fixture_id, match_via)
        values (read_ticket, 5, 'a', 'b', f2, 'GUESS');
        raise exception 'an unknown match method was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, match_via)
        values (read_ticket, 5, 'a', 'b', 'EXACT');
        raise exception 'a match method without a matched fixture was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, matched_fixture_id, unmatched_reason)
        values (read_ticket, 5, 'a', 'b', f2, 'NO_MATCH');
        raise exception 'an unmatched reason on a matched row was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, unmatched_reason)
        values (read_ticket, 5, 'a', 'b', 'BECAUSE');
        raise exception 'an unknown unmatched reason was accepted';
    exception when check_violation then null;
    end;
    foreach t in array array['EXACT', 'ALIAS', 'LOOSE'] loop
        insert into proracle.intake_ticket_rows (intake_id, position, home_team, away_team, matched_fixture_id, match_via)
        values (read_ticket, 10 + array_position(array['EXACT', 'ALIAS', 'LOOSE'], t), 'm-' || t, 'n-' || t, f3, t);
    end loop;

    -- 5. Rejected rows --------------------------------------------------------------------------------
    insert into proracle.intake_rejected_rows (intake_id, position, home_team, away_team, reason) values
        (read_ticket, 21, 'Over', 'Limit', 'OVER_LIMIT'), (read_ticket, 22, null, 'Only away', 'MISSING_TEAM'),
        (read_ticket, 23, 'Same', 'Same', 'SAME_TEAM'), (read_ticket, 24, null, null, 'NOT_A_FIXTURE');
    begin
        insert into proracle.intake_rejected_rows (intake_id, position, reason) values (read_ticket, 25, 'DISLIKED');
        raise exception 'an unknown rejection reason was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_rejected_rows (intake_id, position, reason) values (read_ticket, 21, 'OVER_LIMIT');
        raise exception 'the same rejected position twice was accepted';
    exception when unique_violation then null;
    end;

    -- 6. The pending upload: newest only, expiry, and being used up ------------------------------------
    insert into proracle.intake_pending_uploads (update_id, file_id, file_type, file_size)
    values (900000003, 'FILE-ID-ONE', 'PDF', 1000) returning id into pend1;
    insert into proracle.intake_pending_uploads (update_id, file_id, file_type, file_size)
    values (900000004, 'FILE-ID-TWO', 'IMAGE', null) returning id into pend2;
    execute 'set local role service_role';
    select * into got from proracle.intake_pending_upload;
    if got.id is distinct from pend2 or got.file_id <> 'FILE-ID-TWO' then
        raise exception 'only the newest pending upload may be offered';
    end if;
    if got.expires_at - got.created_at not between interval '59 minutes' and interval '61 minutes' then
        raise exception 'the default expiry is not about 60 minutes';
    end if;
    execute 'reset role';
    insert into proracle.intake_pending_uploads (update_id, file_id, file_type, created_at, expires_at)
    values (900000005, 'FILE-ID-OLD', 'PDF', now() - interval '2 hours', now() - interval '1 hour') returning id into pend3;
    execute 'set local role service_role';
    if (select id from proracle.intake_pending_upload) is distinct from pend2 then
        raise exception 'an older upload must not replace the newest one';
    end if;
    execute 'reset role';
    begin
        insert into proracle.intake_pending_uploads (update_id, file_id, file_type, expires_at)
        values (900000006, 'BAD', 'PDF', now() - interval '1 minute');
        raise exception 'an expiry before creation was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_pending_uploads (update_id, file_id, file_type, expires_at)
        values (900000006, 'BAD', 'PDF', now() + interval '3 hours');
        raise exception 'an expiry more than two hours out was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_pending_uploads (update_id, file_id, file_type)
        values (900000006, 'BAD', 'DOCX');
        raise exception 'an unknown file type was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_pending_uploads (update_id, file_id, file_type, file_size)
        values (900000006, 'BAD', 'PDF', 99999999);
        raise exception 'an over-large file size was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.intake_pending_uploads (update_id, file_id, file_type) values (900000004, 'again', 'PDF');
        raise exception 'two pending uploads for one update were accepted';
    exception when unique_violation then null;
    end;
    -- a read made from the newest pending upload uses it up; it cannot be used twice
    insert into proracle.intake_updates (update_id, kind) values (900000009, 'PDF');
    insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched, pending_upload_id)
    values (900000009, current_date, 'PDF', 'm', 1, 1, 0, pend2);
    execute 'set local role service_role';
    if exists (select 1 from proracle.intake_pending_upload) then
        raise exception 'a pending upload that has been read must not be offered again';
    end if;
    execute 'reset role';
    insert into proracle.intake_updates (update_id, kind) values (900000010, 'PDF');
    begin
        insert into proracle.intake_tickets (update_id, ticket_date, source_kind, vision_model, rows_read, rows_kept, rows_matched, pending_upload_id)
        values (900000010, current_date, 'PDF', 'm', 1, 1, 0, pend2);
        raise exception 'a pending upload was used by two reads';
    exception when unique_violation then null;
    end;
    -- expiry: back-date every pending upload by three hours (this script is the table owner), then the
    -- newest one is an hour past its expiry and nothing may be offered
    update proracle.intake_pending_uploads
       set created_at = created_at - interval '3 hours', expires_at = expires_at - interval '3 hours';
    insert into proracle.intake_updates (update_id, kind) values (900000011, 'PDF');
    insert into proracle.intake_pending_uploads (update_id, file_id, file_type, created_at, expires_at)
    values (900000011, 'FILE-ID-EXPIRED', 'PDF', now() - interval '90 minutes', now() - interval '30 minutes');
    execute 'set local role service_role';
    if exists (select 1 from proracle.intake_pending_upload) then
        raise exception 'an expired pending upload must not be offered';
    end if;
    execute 'reset role';

    -- 7. File hashes: a hash of the bytes only ----------------------------------------------------------
    insert into proracle.intake_file_hashes (file_sha256, intake_id) values (repeat('ab', 32), read_ticket);
    insert into proracle.intake_file_hashes (file_sha256, intake_id) values (repeat('ab', 32), read_slate);
    begin
        insert into proracle.intake_file_hashes (file_sha256, intake_id) values (repeat('ab', 32), read_ticket);
        raise exception 'the same hash for the same read twice was accepted';
    exception when unique_violation then null;
    end;
    foreach t in array array['XYZ', repeat('AB', 32), repeat('a', 63), repeat('a', 65), 'hello world'] loop
        begin
            insert into proracle.intake_file_hashes (file_sha256, intake_id) values (t, read_ticket);
            raise exception 'the hash % was accepted', t;
        exception when check_violation then null;
        end;
    end loop;

    -- 8. Daily token totals ----------------------------------------------------------------------------
    execute 'set local role service_role';
    perform proracle.intake_add_tokens(date '2031-02-01', 1000, 400);
    perform proracle.intake_add_tokens(date '2031-02-01', 500, 100);
    select * into got from proracle.intake_usage where usage_date = date '2031-02-01';
    if got.input_tokens <> 1500 or got.output_tokens <> 500 or got.vision_calls <> 0 then
        raise exception 'token totals wrong: % % %', got.input_tokens, got.output_tokens, got.vision_calls;
    end if;
    perform proracle.intake_reserve_call(date '2031-02-01', 10);
    select * into got from proracle.intake_usage where usage_date = date '2031-02-01';
    if got.vision_calls <> 1 or got.input_tokens <> 1500 then raise exception 'the call counter and tokens interfere'; end if;
    begin
        perform proracle.intake_add_tokens(date '2031-02-01', -1, 0);
        raise exception 'a negative token addition was accepted';
    exception when raise_exception then
        if sqlerrm like 'a negative token addition%' then raise; end if;
    end;
    begin
        update proracle.intake_usage set usage_date = date '2031-03-01' where usage_date = date '2031-02-01';
        raise exception 'service_role changed a usage date';
    exception when insufficient_privilege then null;
    end;
    execute 'reset role';

    -- 9. Ticket kickoff and review times; the send-once review ----------------------------------------------
    insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at, last_kickoff_at, review_due_at)
    values (tk, current_date - 5, 'SIMULATED', 'verify-v1', 's', 1, 1, now() - interval '6 days', now() - interval '5 days', now() - interval '4 days');
    insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
    values (tk_old, current_date - 40, 'SIMULATED', 'verify-v1', 's', 1, 1, now() - interval '41 days');
    begin
        insert into proracle.tickets (ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at, last_kickoff_at)
        values (current_date - 1, 'SIMULATED', 'verify-v2', 's', 1, 1, now() - interval '2 days', now());
        raise exception 'a last kickoff without a review time was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.tickets (ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at, last_kickoff_at, review_due_at)
        values (current_date - 1, 'SIMULATED', 'verify-v3', 's', 1, 1, now() - interval '2 days', now(), now() - interval '1 hour');
        raise exception 'a review time before the last kickoff was accepted';
    exception when check_violation then null;
    end;
    execute 'set local role proracle_runtime';
    insert into proracle.ticket_reviews (ticket_id, completeness, picks_total, picks_settled, summary)
    values (tk, 'PARTIAL', 3, 2, '{"unsettled": 1}');
    begin
        insert into proracle.ticket_reviews (ticket_id, completeness, picks_total, picks_settled, summary)
        values (tk, 'COMPLETE', 3, 3, '{}');
        raise exception 'a second review for one ticket was accepted';
    exception when unique_violation then null;
    end;
    begin
        update proracle.ticket_reviews set completeness = 'COMPLETE' where ticket_id = tk;
        raise exception 'the runtime role updated a review';
    exception when insufficient_privilege then null;
    end;
    begin
        delete from proracle.ticket_reviews where ticket_id = tk;
        raise exception 'the runtime role deleted a review';
    exception when insufficient_privilege then null;
    end;
    execute 'reset role';
    begin
        insert into proracle.ticket_reviews (ticket_id, completeness, picks_total, picks_settled, summary)
        values (tk_old, 'SOMETIMES', 1, 1, '{}');
        raise exception 'an unknown completeness was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.ticket_reviews (ticket_id, completeness, picks_total, picks_settled, summary)
        values (tk_old, 'COMPLETE', 1, 2, '{}');
        raise exception 'more settled picks than picks was accepted';
    exception when check_violation then null;
    end;
    begin
        insert into proracle.ticket_reviews (ticket_id, completeness, picks_total, picks_settled, summary)
        values (tk_old, 'COMPLETE', 1, 1, '[1]');
        raise exception 'a summary that is not an object was accepted';
    exception when check_violation then null;
    end;
    -- the function may read reviews but not write them
    execute 'set local role service_role';
    if (select count(*) from proracle.ticket_reviews where ticket_id = tk) <> 1 then raise exception 'service_role cannot read reviews'; end if;
    begin
        insert into proracle.ticket_reviews (ticket_id, completeness, picks_total, picks_settled, summary)
        values (tk_old, 'COMPLETE', 1, 1, '{}');
        raise exception 'service_role wrote a review';
    exception when insufficient_privilege then null;
    end;
    execute 'reset role';
    -- the runtime role cannot write an intake read, read hashes or pending uploads
    execute 'set local role proracle_runtime';
    begin
        perform 1 from proracle.intake_file_hashes;
        raise exception 'the runtime role read file hashes';
    exception when insufficient_privilege then null;
    end;
    begin
        perform 1 from proracle.intake_pending_uploads;
        raise exception 'the runtime role read pending uploads';
    exception when insufficient_privilege then null;
    end;
    begin
        insert into proracle.intake_rejected_rows (intake_id, position, reason) values (read_ticket, 30, 'OVER_LIMIT');
        raise exception 'the runtime role wrote a rejected row';
    exception when insufficient_privilege then null;
    end;
    begin
        update proracle.intake_tickets set kind = 'SLATE' where id = read_old;
        raise exception 'the runtime role updated a read';
    exception when insufficient_privilege then null;
    end;
    if (select count(*) from proracle.intake_rejected_rows where intake_id = read_ticket) <> 4 then
        raise exception 'the runtime role should read the four rejected rows';
    end if;
    execute 'reset role';

    -- 10. The recent window: last 28 days against the 28 before, on complete tickets ------------------------
    insert into proracle.predictions (
        id, fixture_id, contract_version, predicted_at, kickoff_at, model_provider, model_name,
        model_version, prompt_version, input_schema_version, input_snapshot_hash,
        home_win_probability, draw_probability, away_win_probability
    ) values
        (pred1, f1, '1.0', now() - interval '6 days', now() - interval '5 days', 'synthetic', 'test-model', '1', 'p', 'features-v1', repeat('a', 64), 0.5, 0.3, 0.2),
        (pred2, f2, '1.0', now() - interval '41 days', now() - interval '40 days', 'synthetic', 'test-model', '1', 'p', 'features-v1', repeat('b', 64), 0.5, 0.3, 0.2);
    insert into proracle.results (fixture_id, status, actual_outcome, observed_at, finalized_at, source_name) values
        (f1, 'FINAL', 'HOME_WIN', now() - interval '5 days' + interval '3 hours', now() - interval '5 days' + interval '3 hours', 'synthetic'),
        (f2, 'FINAL', 'DRAW', now() - interval '40 days' + interval '3 hours', now() - interval '40 days' + interval '3 hours', 'synthetic');
    insert into proracle.ticket_fixtures (ticket_id, fixture_id, position) values (tk, f1, 1), (tk_old, f2, 1);
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, source_prediction_id, kickoff_at, frozen_at) values
        (tk, f1, 'A', 'verify-a', 'HOME_WIN', pred1, now() - interval '5 days', now() - interval '6 days'),
        (tk_old, f2, 'A', 'verify-a', 'HOME_WIN', pred2, now() - interval '40 days', now() - interval '41 days');
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, forced, kickoff_at, frozen_at) values
        (tk, f1, 'C', 'verify-c', 'DRAW', true, now() - interval '5 days', now() - interval '6 days'),
        (tk_old, f2, 'C', 'verify-c', 'DRAW', true, now() - interval '40 days', now() - interval '41 days');
    execute 'set local role service_role';
    for got in select * from proracle.intake_recent_window where ticket_kind = 'SIMULATED' and arm_version like 'verify-%' order by arm, window_name loop
        if got.arm = 'A' and got.window_name = 'RECENT' and (got.picks_settled, got.picks_right, got.a_right_same_fixtures) <> (1, 1, 1) then
            raise exception 'arm A recent window wrong'; end if;
        if got.arm = 'A' and got.window_name = 'PRIOR' and (got.picks_settled, got.picks_right, got.a_right_same_fixtures) <> (1, 0, 0) then
            raise exception 'arm A prior window wrong'; end if;
        if got.arm = 'C' and got.window_name = 'RECENT' and (got.picks_settled, got.picks_right, got.a_right_same_fixtures) <> (1, 0, 1) then
            raise exception 'arm C recent window wrong'; end if;
        if got.arm = 'C' and got.window_name = 'PRIOR' and (got.picks_settled, got.picks_right, got.a_right_same_fixtures) <> (1, 1, 0) then
            raise exception 'arm C prior window wrong'; end if;
    end loop;
    if (select count(*) from proracle.intake_recent_window where ticket_kind = 'SIMULATED' and arm_version like 'verify-%') <> 4 then
        raise exception 'the recent window should have arms A and C in both windows';
    end if;
    execute 'reset role';
    -- a pick older than 56 days drops out of both windows
    insert into proracle.fixtures (id, home_team, away_team, kickoff_at, source_name, source_event_key)
    values ('00000000-0000-4000-8000-0000000000f4', 'Old', 'Older', now() - interval '70 days', 'synthetic', 'ts-4');
    insert into proracle.tickets (id, ticket_date, ticket_kind, design_version, seed, pool_size, fixture_count, frozen_at)
    values ('00000000-0000-4000-8000-0000000000d4', current_date - 70, 'SIMULATED', 'verify-v1', 's', 1, 1, now() - interval '71 days');
    insert into proracle.ticket_fixtures (ticket_id, fixture_id, position)
    values ('00000000-0000-4000-8000-0000000000d4', '00000000-0000-4000-8000-0000000000f4', 1);
    insert into proracle.predictions (
        id, fixture_id, contract_version, predicted_at, kickoff_at, model_provider, model_name,
        model_version, prompt_version, input_schema_version, input_snapshot_hash,
        home_win_probability, draw_probability, away_win_probability
    ) values ('00000000-0000-4000-8000-0000000000c4', '00000000-0000-4000-8000-0000000000f4', '1.0', now() - interval '71 days', now() - interval '70 days', 'synthetic', 'test-model', '1', 'p', 'features-v1', repeat('c', 64), 0.5, 0.3, 0.2);
    insert into proracle.results (fixture_id, status, actual_outcome, observed_at, finalized_at, source_name)
    values ('00000000-0000-4000-8000-0000000000f4', 'FINAL', 'DRAW', now() - interval '70 days', now() - interval '70 days', 'synthetic');
    insert into proracle.ticket_arm_picks (ticket_id, fixture_id, arm, arm_version, pick, source_prediction_id, kickoff_at, frozen_at)
    values ('00000000-0000-4000-8000-0000000000d4', '00000000-0000-4000-8000-0000000000f4', 'A', 'verify-a', 'DRAW', '00000000-0000-4000-8000-0000000000c4', now() - interval '70 days', now() - interval '71 days');
    execute 'set local role service_role';
    if (select count(*) from proracle.intake_recent_window where ticket_kind = 'SIMULATED' and arm_version like 'verify-%') <> 4 then
        raise exception 'a pick older than 56 days must not appear';
    end if;
    execute 'reset role';

    -- 11. The new audit actions -------------------------------------------------------------------------
    execute 'set local role service_role';
    foreach t in array array['SLATE', 'PENDING', 'REUSED', 'TOKEN_CAP', 'REVIEW', 'ELI5'] loop
        insert into proracle.intake_audit (action, outcome, detail) values (t, 'OK', 'ok');
    end loop;
    begin
        insert into proracle.intake_audit (action, outcome) values ('GOSSIP', 'OK');
        raise exception 'an unknown audit action was accepted';
    exception when check_violation then null;
    end;
    execute 'reset role';
end
$verify$;

rollback;
select 'verify_ticket_slate: all checks passed' as result;
