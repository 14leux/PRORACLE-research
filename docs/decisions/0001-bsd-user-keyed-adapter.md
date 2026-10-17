# Decision 0001: User-keyed BSD adapter and safe failures

**Status:** Accepted for B4 implementation, 2026-10-07.

## Context

PRORACLE needs one optional source-backed path that an operator can run against their own provider account and Supabase project. The contributor path must continue to work offline. B0 approved BSD football API v2 provisionally, a narrow fixture/final-score allowlist, the existing B3 add-one frequency baseline, installation-local retention, and a separate commercial-rights review gate.

## Decision

- The first adapter uses BSD's documented `/api/v2/events/` endpoint and standard-library HTTP/JSON support. Each operator supplies their own token through the local environment.
- Normalize only provider event key, team labels, competition reference/name, UTC kickoff, lifecycle status, and final home/away scores needed to derive the 1X2 result. Ignore all other response keys, including odds, forecasts, media, and statistics. Never persist raw responses.
- Paginate with documented `limit`/`offset`, cap page size at 200, use bounded timeouts, and retry only transient per-IP limits or server/network failures within a small configured maximum. Respect `Retry-After`; a daily-quota exhaustion stops the run instead of retrying immediately.
- Record the local first-observed-final/retrieval time as result availability when the source supplies no verifiable publication timestamp. Do not treat kickoff or today's historical backfill as proof of historical availability.
- Use the B3 add-one frequency predictor only; add no model SDK or engineered provider features. Persist normalized records and immutable predictions only to the operator's own Supabase instance.
- Persist failures as allowlisted stage/code/retryable fields only. The forward-only failure-event table is append-only for the runtime role, protected by RLS, and receives explicit table-scoped privileges. API roles `anon` and `authenticated` receive no access.
- Store BSD event identifiers only as installation-local join keys, subject to the operator's current terms. Do not publish provider-derived rows, API responses, or identifiers in examples or CI artifacts. BSD terms do not establish blanket commercial rights; commercial use requires qualified review of provider and local legal obligations.

## Trade-offs

Stable error codes provide weaker diagnosis than raw provider messages but avoid durable disclosure. The frequency baseline is simple and has no inference dependency or claim of predictive skill; B5 prospective evaluation must determine whether later models improve it. Local first-seen timestamps support forward-looking evaluation but do not reconstruct historical availability.

## Revisit when

Official BSD response fields or terms change; the requested scope expands beyond the allowlist; the failure contract changes; or a contributor proposes another licensed source/model with a separately reviewed dependency and rights basis.

## References

- [BSD football events documentation](https://sports.bzzoiro.com/docs/football/events/)
- [BSD API conventions and rate limits](https://sports.bzzoiro.com/docs/conventions/)
- [BSD data usage and license](https://sports.bzzoiro.com/docs/api-license/)
- [Supabase Data API table exposure change](https://supabase.com/changelog/45329-breaking-change-tables-not-exposed-to-data-and-graphql-api-automatically)
