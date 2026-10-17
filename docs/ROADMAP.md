# Roadmap

The roadmap is a set of current public work areas, not a copy of the private project's milestones or operational history. Issues and pull requests are the source for active work and status.

## Foundation

- [x] Define public scope, license intent, data boundaries, and contributor process.
- [ ] Publish the audited first file manifest and clean-history repository.
- [x] Implement a model-free offline evaluation path using synthetic fixtures.
- [x] Add the blank local Supabase bootstrap migration and setup instructions; verify direct grants/RLS, role behavior, and Security Advisor findings on PostgreSQL 17.6.

## Prediction baseline

- [x] Build a minimal, self-hosted fixture-to-prediction path using the add-one historical-frequency baseline. The adapters, offline mocks, blank-local-database migration, and rollback-only access verifier are implemented and locally verified. Live provider credentials and a hosted operator deployment have not been tested.
- [x] Add dependency-free validation for the agreed immutable football 1X2 prediction and failure-event contracts.
- [x] Add a chronological evaluation harness and smoothed historical-frequency baseline.
- [x] Add a deterministic synthetic fixture-to-prediction-to-result flow with fake service boundaries and a time-aware add-one baseline.
- [x] Report accuracy, log loss, Brier score, calibration bins, baseline comparisons, and limitations.
- [x] Add uncertainty estimates and sample-size guidance (`proracle.retrospective`): Wilson and seeded paired-bootstrap intervals, descriptive-only flag under 30 fixtures. Verified offline; not yet run on real stored rows.

## Self-hosting and reports

- [/] Support user-owned provider and Supabase configuration. B4 reads the operator's BSD/Supabase settings for the frequency baseline; no optional model adapter is included, and live provider/hosted deployment verification remains open.
- [x] Add optional reporting to a user's own Telegram bot (`proracle.telegram_report`: preview by default, offline tests; a real send to a test bot is not yet verified).
- [x] Document scheduling, cost drivers, limits, and safe failure behavior ([docs/OPERATIONS.md](OPERATIONS.md)).

## Community research

- [ ] Open reproducible tasks for coverage, data quality, calibration, draw errors, and model comparison.
- [ ] Publish null and negative results as well as improvements.
- [x] Document the separate betting-economics research track: ground rules, definitions and assumption ledger ([docs/BETTING_ECONOMICS.md](BETTING_ECONOMICS.md)). Documentation only; no code or price data.
- [ ] Consider a separate paper-only betting-economics study after data rights and evaluation design are reviewed.
