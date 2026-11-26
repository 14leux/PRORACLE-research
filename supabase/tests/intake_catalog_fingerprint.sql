-- One md5 per catalog facet of the P0 capture and intake objects (tables, views, function). Run
-- before and after applying the intake migration a second time: identical output means the second
-- application changed nothing.
with t(name) as (
    values ('match_scores'), ('competition_countries'), ('tickets'), ('ticket_fixtures'), ('ticket_arm_picks'),
           ('intake_updates'), ('intake_usage'), ('intake_tickets'), ('intake_ticket_rows'), ('intake_rules'),
           ('intake_rule_events'), ('intake_audit')
)
select facet, md5(coalesce(string_agg(item, E'\n' order by item), '')) as fingerprint, count(*) as items
from (
    select 'columns' as facet,
           format('%s.%s %s %s %s', c.table_name, c.column_name, c.data_type, c.is_nullable, coalesce(c.column_default, ''))
           as item
      from information_schema.columns c join t on t.name = c.table_name
     where c.table_schema = 'proracle'
    union all
    select 'constraints', format('%s %s %s', cl.relname, con.conname, pg_get_constraintdef(con.oid))
      from pg_constraint con
      join pg_class cl on cl.oid = con.conrelid
      join pg_namespace n on n.oid = cl.relnamespace and n.nspname = 'proracle'
      join t on t.name = cl.relname
    union all
    select 'indexes', indexdef from pg_indexes i join t on t.name = i.tablename where i.schemaname = 'proracle'
    union all
    select 'policies',
           format('%s %s %s %s %s %s', p.tablename, p.policyname, p.cmd, p.roles::text, p.qual, p.with_check)
      from pg_policies p join t on t.name = p.tablename where p.schemaname = 'proracle'
    union all
    select 'acl_rls_comment',
           format('%s %s %s %s', cl.relname, coalesce(cl.relacl::text, ''), cl.relrowsecurity,
                  coalesce(obj_description(cl.oid, 'pg_class'), ''))
      from pg_class cl
      join pg_namespace n on n.oid = cl.relnamespace and n.nspname = 'proracle'
     where cl.relname in (select name from t)
        or cl.relname in ('intake_active_rule', 'intake_pending_rule', 'intake_arm_scoreboard')
    union all
    select 'views', format('%s %s %s', v.viewname, pg_get_viewdef(format('proracle.%I', v.viewname)::regclass),
                           (select coalesce(string_agg(o, ','), '') from pg_class c2, unnest(c2.reloptions) o where c2.oid = format('proracle.%I', v.viewname)::regclass))
      from pg_views v
     where v.schemaname = 'proracle' and v.viewname in ('intake_active_rule', 'intake_pending_rule', 'intake_arm_scoreboard')
    union all
    select 'function', pg_get_functiondef(p.oid) || coalesce(p.proacl::text, '')
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace and n.nspname = 'proracle'
     where p.proname = 'intake_reserve_call'
) facets
group by facet
order by facet;
