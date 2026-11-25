-- One md5 per catalog facet of the P0 capture tables. Run before and after applying the migration
-- a second time: identical output means the second application changed nothing.
with t(name) as (
    values ('match_scores'), ('competition_countries'), ('tickets'), ('ticket_fixtures'), ('ticket_arm_picks')
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
    select 'acl_and_rls',
           format('%s %s %s', cl.relname, coalesce(cl.relacl::text, ''), cl.relrowsecurity)
      from pg_class cl
      join pg_namespace n on n.oid = cl.relnamespace and n.nspname = 'proracle'
      join t on t.name = cl.relname
    union all
    select 'comments', format('%s %s', cl.relname, coalesce(obj_description(cl.oid, 'pg_class'), ''))
      from pg_class cl
      join pg_namespace n on n.oid = cl.relnamespace and n.nspname = 'proracle'
      join t on t.name = cl.relname
) facets
group by facet
order by facet;
