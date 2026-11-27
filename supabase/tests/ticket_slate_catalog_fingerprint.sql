-- One md5 per catalog facet of the tickets-and-slates migration's objects: the new tables, views and
-- function, and the existing tables it extends. Run before and after applying the migration a second
-- time: identical output means the second application changed nothing.
with t(name) as (
    values ('intake_tickets'), ('intake_ticket_rows'), ('intake_usage'), ('intake_audit'), ('tickets'),
           ('intake_rejected_rows'), ('intake_pending_uploads'), ('intake_file_hashes'), ('ticket_reviews')
),
v(name) as (
    values ('intake_pending_upload'), ('intake_recent_window')
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
     where cl.relname in (select name from t) or cl.relname in (select name from v)
    union all
    select 'column_acl',
           format('%s.%s %s', cl.relname, a.attname, a.attacl::text)
      from pg_attribute a
      join pg_class cl on cl.oid = a.attrelid
      join pg_namespace n on n.oid = cl.relnamespace and n.nspname = 'proracle'
      join t on t.name = cl.relname
     where a.attacl is not null and not a.attisdropped
    union all
    select 'views', format('%s %s %s', vw.viewname, pg_get_viewdef(format('proracle.%I', vw.viewname)::regclass),
                           (select coalesce(string_agg(o, ','), '') from pg_class c2, unnest(c2.reloptions) o where c2.oid = format('proracle.%I', vw.viewname)::regclass))
      from pg_views vw join v on v.name = vw.viewname
     where vw.schemaname = 'proracle'
    union all
    select 'function', pg_get_functiondef(p.oid) || coalesce(p.proacl::text, '')
      from pg_proc p join pg_namespace n on n.oid = p.pronamespace and n.nspname = 'proracle'
     where p.proname in ('intake_add_tokens', 'intake_reserve_call')
) facets
group by facet
order by facet;
