-- Rollback for the window and ticket views migration (20261202000000): drops the two views it added. They
-- hold no data of their own, so nothing can be lost. Run it with psql against a DATABASE COPY first (see
-- docs/DATABASE.md), never straight against production. One transaction.
--
--   psql -X -v ON_ERROR_STOP=1 -f supabase/tests/rollback_window_ticket_views.sql
begin;

drop view if exists proracle.intake_window_summary;
drop view if exists proracle.intake_ticket_hits;

commit;
select 'rollback_window_ticket_views: done' as result;
