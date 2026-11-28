-- Rollback for the slate-score migration (20261130000000): removes the one view it added. It holds no data
-- of its own, so nothing can be lost. Run it with psql against a DATABASE COPY first (see docs/DATABASE.md),
-- never straight against production. One transaction.
--
--   psql -X -v ON_ERROR_STOP=1 -f supabase/tests/rollback_slate_score.sql
begin;

drop view if exists proracle.intake_slate_score;

commit;
select 'rollback_slate_score: done' as result;
