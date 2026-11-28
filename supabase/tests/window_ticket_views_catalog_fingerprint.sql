-- One md5 per catalog facet of the window and ticket views migration's two views (definitions, options,
-- comments and grants). Run before and after applying the migration a second time: identical output means
-- the second application changed nothing.
select facet, md5(coalesce(string_agg(item, E'\n' order by item), '')) as fingerprint, count(*) as items
from (
    select 'views' as facet,
           format('%s %s %s', vw.viewname, pg_get_viewdef(format('proracle.%I', vw.viewname)::regclass),
                  (select coalesce(string_agg(o, ','), '')
                     from pg_class c2, unnest(c2.reloptions) o
                    where c2.oid = format('proracle.%I', vw.viewname)::regclass)) as item
      from pg_views vw
     where vw.schemaname = 'proracle' and vw.viewname in ('intake_window_summary', 'intake_ticket_hits')
    union all
    select 'acl_comment',
           format('%s %s %s', cl.relname, coalesce(cl.relacl::text, ''), coalesce(obj_description(cl.oid, 'pg_class'), ''))
      from pg_class cl
      join pg_namespace n on n.oid = cl.relnamespace and n.nspname = 'proracle'
     where cl.relname in ('intake_window_summary', 'intake_ticket_hits')
) facets
group by facet
order by facet;
