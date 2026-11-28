-- PRORACLE: a read-only score of settled slates, for the plain-language performance message (/eli5).
--
-- Additive and repeatable: one view, nothing else. It removes and changes nothing the deployed function
-- or the workflows read. It runs as the caller (security_invoker), so it sees only what the caller's
-- own grants allow; only service_role (the intake function) is granted it.
--
-- One row, counts only: how many slate reads there are, how many distinct fixtures they matched (each
-- counted once however many uploads hold it), how many of those have the rating model's earliest
-- pre-kickoff pick and a current final result (settled), how often that pick was right, and, on the
-- settled fixtures where all three prices were printed, how often the model and the market favourite
-- were right. Same definitions as proracle/slate.py. No names, no prices, no stakes, no profit.
--
-- Rollback: supabase/tests/rollback_slate_score.sql

create or replace view proracle.intake_slate_score with (security_invoker = true) as
with slate_rows as (
    select r.matched_fixture_id as fixture_id, r.home_odds, r.draw_odds, r.away_odds,
           i.id as read_id, i.received_at, r.position
      from proracle.intake_ticket_rows r
      join proracle.intake_tickets i on i.id = r.intake_id
     where i.kind = 'SLATE' and r.read_kind = 'SLATE' and r.matched_fixture_id is not null
),
-- each fixture once; it keeps the earliest upload that printed all three prices, else the earliest upload
fixtures_once as (
    select distinct on (fixture_id) fixture_id, home_odds, draw_odds, away_odds
      from slate_rows
     order by fixture_id, (home_odds is null), received_at, read_id, position
),
-- the rating model's earliest prediction made before kickoff (the model name the Python side scores)
model_pick as (
    select distinct on (p.fixture_id) p.fixture_id,
           case when p.home_win_probability >= p.draw_probability and p.home_win_probability >= p.away_win_probability then 'HOME_WIN'
                when p.draw_probability >= p.away_win_probability then 'DRAW'
                else 'AWAY_WIN' end as pick
      from proracle.predictions p
      join fixtures_once f on f.fixture_id = p.fixture_id
     where p.model_name = 'elo_davidson_recalibrated' and p.predicted_at < p.kickoff_at
     order by p.fixture_id, p.predicted_at, p.id
),
current_results as (
    select r.fixture_id, r.actual_outcome
      from proracle.results r
     where r.status = 'FINAL'
       and not exists (select 1 from proracle.results s where s.supersedes_result_id = r.id)
),
settled as (
    select f.fixture_id, m.pick, c.actual_outcome,
           (f.home_odds is not null) as has_prices,
           case when f.home_odds is null then null
                when f.home_odds <= f.draw_odds and f.home_odds <= f.away_odds then 'HOME_WIN'
                when f.draw_odds <= f.away_odds then 'DRAW'
                else 'AWAY_WIN' end as market_pick
      from fixtures_once f
      join model_pick m on m.fixture_id = f.fixture_id
      join current_results c on c.fixture_id = f.fixture_id
)
select (select count(distinct read_id) from slate_rows) as slate_reads,
       (select count(*) from fixtures_once) as fixtures,
       count(*) as settled,
       count(*) filter (where pick = actual_outcome) as model_hits,
       count(*) filter (where has_prices) as compared,
       count(*) filter (where has_prices and pick = actual_outcome) as compared_model_hits,
       count(*) filter (where has_prices and market_pick = actual_outcome) as compared_market_hits
  from settled;

comment on view proracle.intake_slate_score is
    'Counts only: settled slate fixtures (each once), the rating model hits, and model against market favourite where all three prices were printed. Read by the intake function for /eli5.';

revoke all privileges on table proracle.intake_slate_score from public, anon, authenticated, service_role, proracle_runtime;
grant select on table proracle.intake_slate_score to service_role;
