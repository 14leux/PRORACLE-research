# Planned Architecture

The first self-hosted release is intended to support a small football prediction flow:

1. Obtain upcoming fixtures through a source adapter configured by the user.
2. Enrich fixtures with permitted, prediction-time information.
3. Validate and tag complete fixtures.
4. Generate and store probability-bearing predictions.
5. Record results and evaluate predictions after completion.
6. Optionally send reports through the user's own Telegram bot.

The intended persistent store is the user's own Supabase project. Provider credentials, model credentials, and Telegram tokens belong to the user and must be stored outside the repository in local ignored configuration or a protected runtime secret store. No example should contain a real project URL or key.

In the deployed design, independently scheduled stages exchange persistent state through that installation's Supabase project. `proracle.flow.run_offline_flow` is a single-process synthetic developer/demo harness that exercises replaceable boundaries; it is not production stage orchestration and does not establish a different inter-stage communication contract.

Contributor checks should use synthetic fixtures and fake provider responses. They should run without Supabase, external APIs, model calls, or secrets. Production data access and deployment are maintainer-controlled and kept separate from pull-request workflows.

The public prediction path will not fetch or store bookmaker odds, calculate betting edges, simulate stakes, or settle bankrolls. Betting economics may be explored in separate documented research using properly licensed data and evidence of executable prices.

This document describes the intended design, not a claim that these components are implemented or verified. See the README for current project status.

## Proposed prediction contract and minimal records

**Status: contract agreed by the maintainer on 2026-10-06.** Dependency-free record validators, the initial schema migration, and a synthetic offline domain flow are implemented. Live prediction generation, live result ingestion, and provider/model adapters remain pending. This contract gives model contributors a stable output shape and keeps predictions separate from later match results. It is scoped to football 1X2 (home win, draw, away win) for the first runnable path. Other sports or markets need a separately versioned outcome schema.

### Successful prediction output

A prediction is one immutable, timestamped probability vector for one fixture:

```json
{
  "contract_version": "1.0",
  "prediction_id": "00000000-0000-4000-8000-000000000001",
  "fixture_id": "00000000-0000-4000-8000-000000000002",
  "predicted_at": "2026-10-06T12:00:00Z",
  "kickoff_at": "2026-10-06T18:00:00Z",
  "model": {
    "provider": "provider identifier",
    "name": "model identifier",
    "version": "model version or configured alias"
  },
  "prompt_version": "prompt identifier and version",
  "input_schema_version": "features-v1",
  "input_snapshot_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "probabilities": {
    "HOME_WIN": 0.48,
    "DRAW": 0.27,
    "AWAY_WIN": 0.25
  }
}
```

The example UUIDs and snapshot digest are fabricated format examples only; they do not identify a real fixture or feature set.

Required validation:

- `contract_version` and `input_schema_version` are recognized (`features-v1` for the frequency baseline, `results-v1` for the [rating model](RATING_MODEL.md)), and IDs are non-empty local identifiers.
- All timestamps are ISO-8601 with an explicit offset and are stored/compared in UTC. `predicted_at` must be strictly earlier than `kickoff_at`.
- `probabilities` contains exactly `HOME_WIN`, `DRAW`, and `AWAY_WIN`; each is finite and between 0 and 1 inclusive; their sum is 1 within an absolute tolerance of `1e-6`.
- A successful record contains no actual result, bookmaker price, market-implied probability, stake, bet decision, or free-form model rationale. A failed/invalid model call is an explicit error event and never a fabricated probability row.
- The top-probability outcome is derived for display and accuracy scoring, not separately stored as an independent model claim. Do not store a scalar `confidence` alongside the probability vector; it duplicates or obscures what those probabilities mean.
- The local snapshot hash supports integrity checks but does not make data reproducible by itself. Reproduction also requires the permitted feature snapshot, source and retrieval timestamps, feature transformation version, model/version, prompt version, and configuration. Never publish restricted source rows just to satisfy reproducibility.

Failure events use a separate, minimal shape and never carry raw exception strings or provider response/prompt content:

```json
{
  "contract_version": "1.0",
  "event_type": "prediction_failed",
  "event_id": "00000000-0000-4000-8000-000000000003",
  "fixture_id": "00000000-0000-4000-8000-000000000002",
  "occurred_at": "2026-10-06T12:01:00Z",
  "kickoff_at": "2026-10-06T18:00:00Z",
  "stage": "MODEL_REQUEST",
  "error_code": "MODEL_TIMEOUT",
  "retryable": true
}
```

Allowed failure stages are `INPUT_VALIDATION`, `MODEL_REQUEST`, `RESPONSE_VALIDATION`, and `PERSISTENCE`. Error codes are stable, stage-specific identifiers: `INVALID_INPUT`, `FEATURES_MISSING`, `FEATURES_STALE`; `MODEL_TIMEOUT`, `MODEL_RATE_LIMIT`, `MODEL_UNAVAILABLE`, `COST_LIMIT_REACHED`; `MODEL_INVALID_OUTPUT`; or `PERSISTENCE_FAILED`. Unknown codes and codes used at the wrong stage are rejected, which prevents arbitrary text from being smuggled into the event. Failure time must also be strictly before kickoff for this prematch contract.

The B3 synthetic helper validates a generic non-empty mapping of finite numeric features (maximum 64 keys, lowercase snake_case names) and hashes canonical sorted JSON encoded as UTF-8. This is only an offline contract exercise; it does not define the eventual provider feature schema. The hash must not include credentials, hidden provider metadata, or a raw provider response. If public/reproducible access to feature values is not permitted, retain the snapshot privately and publish only a rights-cleared aggregate or synthetic example.

`input_snapshot_hash` is an installation-local audit field by default, not a public identifier or anonymization mechanism. Do not publish it unless the underlying feature set and the risk of guessing low-entropy inputs have been reviewed; omit it from shared exports otherwise.

### Minimal logical data model

1. **Fixture** — local UUID; sport; competition identifier (nullable where unavailable); home/away display names; scheduled kickoff in UTC; lifecycle status; source identifier and source event key held as installation-local metadata where permitted. Do not treat an upstream identifier as globally public or as permission to redistribute source data.
2. **Prediction** — the successful contract above, append-only. Re-running or correcting a prediction creates a new prediction row; it does not overwrite a historical forecast. Multiple predictions for one fixture are allowed and remain distinguishable by timestamp and model/prompt versions.
3. **Final result** — local result-record ID; fixture ID; result status (`FINAL`, `ABANDONED`, `CANCELLED`, or `VOID`); actual 1X2 outcome only when status is `FINAL`; finalization timestamp; source identifier and retrieval timestamp. A corrected result is a new version/record with an auditable supersession link, not a silent edit. Non-final statuses are excluded from scored cohorts.

The feature snapshot can initially be an installation-local file/object or an explicitly rights-cleared normalized record. A fourth persistent feature table is not required until the first-source rights, retention rules, and exact input feature contract are approved. Avoid storing raw API responses by default.

### Evaluation boundary

Offline prediction artifacts contain predictions only. A separate evaluation step joins eligible predictions to final results by local fixture ID after both are available, applies the chronological split, and emits the evaluator's current retrospective row shape (`fixture_id`, `kickoff_at`, `prediction_at`, `probabilities`, `actual_outcome`). Training rows may also include `available_at`; when present it must be timezone-aware, no earlier than kickoff, and strictly before every evaluated prediction. Legacy rows without it use kickoff as a compatibility proxy, not evidence of the actual time the outcome became available. The synthetic add-one frequency baseline uses only outcomes available before each prediction it creates. Thus the present evaluator input is an evaluation record, not the live prediction API or prediction-table schema. See [the evaluation protocol](EVALUATION_PROTOCOL.md).

### Identity, privacy, and first database boundary

For a self-hosted first release, local UUIDs are generated by that installation; no maintainer production IDs or credentials are reused. Team/competition labels and feature values must come from each user's approved provider use and remain subject to that provider's terms. The database belongs to its operator. Service credentials stay server-side, and table grants/RLS must be verified on a blank Supabase project before describing the schema as safe to deploy. A multi-user hosted service, cross-tenant permissions, public fixture identifiers, and provider-data redistribution are outside this minimal contract.

### Decisions intentionally left open

- Exact first-source rights, competition coverage, provider attribution, data retention, and use of source identifiers.
- Feature names, units, null/missing-value semantics, maximum staleness, and eligibility thresholds.
- Model SDK/provider, structured-output mechanism, retry/cost limits, and operational error storage.
- Whether features remain installation-local or move into the database after the rights review.

Resolve these in a source-specific implementation plan before adding migrations or live integrations. The offline evaluator's current tests continue to define its implemented parsing behavior until that evaluator is separately changed and tested.

**B1 implementation status:** `proracle/contracts.py` implements the dependency-free validators `parse_prediction_record()` and `parse_prediction_failure_event()`. Synthetic unit and exhaustive probability-simplex tests run offline. This establishes the record boundary only; it does not generate predictions, calculate the feature hash, connect to a provider/database, or make the wider prediction flow runnable.

**B2 implementation status:** Before first release, the migration baseline was amended to create the four PRORACLE tables in an isolated `proracle` schema. The adapter selects that schema through the Supabase REST profile headers; local CLI config exposes it and the hosted setup guide explains the API setting. On 2026-10-15, both migrations applied to a fresh isolated local Supabase project, the rollback-only SQL verifier passed including same-name legacy-table coexistence, and a Data API smoke check confirmed service-role insert/read plus anonymous denial. The Security Advisor returned only four expected INFO findings for RLS-enabled tables without client policies. The 84-test offline suite passed. Earlier 2026-10-07 verification referred to the superseded `public`-schema baseline. No hosted or production project was used; see [local database setup](DATABASE.md) for the verification recipe.

**B3 implementation status:** The dependency-free `proracle.domain` and `proracle.flow` modules normalize allowlisted fixtures, validate/hash generic synthetic numeric features, create immutable add-one frequency predictions, attach matching final results, and export evaluation rows through injected fake-service interfaces. `python -m proracle.demo` runs the local synthetic path. No network, database, external provider/model, or credentials are used. Fabricated output is a plumbing example only and does not demonstrate sports-prediction skill or profitability. See [the evaluation protocol](EVALUATION_PROTOCOL.md) for the timing limitations of legacy rows without `available_at`.

**B5 implementation status:** `proracle.retrospective` builds point-in-time cohorts from stored predictions and every result version, resolves corrections across all statuses (so a voided result also leaves the baseline), and reports with uncertainty and slices. `SupabaseRESTStore.training_results()` now uses the same resolution. Offline tests cover the logic. On 2026-10-08 a live run against a blank local Supabase (CLI 2.117.0) seeded synthetic fixtures, predictions, a rerun, a void correction, a late label correction and an unresolved fixture; the PostgREST embeds worked, the report excluded exactly the expected fixtures, the exported JSONL replayed through `proracle.evaluate` to identical metrics, the cohort digest was stable across runs, and anonymous reads were refused (401). No hosted project or real provider data was used.

**B4 implementation status:** `proracle.bsd` implements an optional, user-keyed BSD v2 events client with bounded pagination, safe retry mapping, and fixture/regulation-score allowlisting. `proracle.supabase_adapter` maps fixtures, versioned results, predictions, and safe failure events to the operator's own Supabase Data API. `proracle.self_hosted` previews by default and requires `--execute` for writes; its first predictor is the B3 add-one outcome-frequency baseline. The 84-test offline suite passes. The forward-only B4 migration and extended rollback-only SQL verifier passed against a blank local PRORACLE database after applying both candidate migrations; all four application tables remained empty after rollback. No live provider or hosted database was used. Live provider behavior, source-rights compatibility, and commercial use remain unverified. No real sports-performance result is claimed.
