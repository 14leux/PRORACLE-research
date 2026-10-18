# PRORACLE — Your Pro Sports Oracle

PRORACLE is an open project for building and honestly evaluating football match predictions. It gives you a small, self-hosted pipeline (fixtures in, probability-bearing predictions out, results scored later) and a strict evaluation protocol that reports sample sizes, uncertainty and exclusions, so a claim can be checked instead of believed.

> **Release status: first public release, 2026-10-17.** The offline suite and disposable local Supabase migration, grant, and row-level-security checks pass. No hosted database, live provider account, or real-data prediction run has been verified. Nothing here is evidence of real match-prediction skill.

## What it does today

- **Validates predictions.** Dependency-free record contracts for one immutable probability vector per fixture (home, draw, away), timestamped before kickoff, with explicit failure events instead of silent bad data.
- **Evaluates them honestly.** A chronological evaluator reports accuracy, log loss, Brier score, calibration bins and the difference from a simple baseline. The retrospective report adds 95% intervals, slices, exclusions and a "descriptive only" flag for small samples.
- **Runs offline.** A synthetic end-to-end demo and the whole test suite need no accounts, no network and no credentials.
- **Rates teams (no AI).** A walk-forward Elo rating with a Davidson draw term, shrinkage and recalibration, written as a second model identity next to the baseline on every run. It is the bar any AI predictor must beat. See [the rating model](docs/RATING_MODEL.md) and the pre-registered [evidence bar](docs/decisions/0005-ai-predictor-evidence-bar.md).
- **Self-hosts (optional).** A bounded fixture and result adapter for BSD, a smoothed historical-frequency baseline predictor, an isolated `proracle` Supabase schema with grants and row-level security, a read-only retrospective report, and a standalone Telegram report sender, all using your own keys.

## What it does not do

- **It does not include bookmaker odds, staking, bankroll simulation or betting advice**, and bookmaker prices are not prediction inputs.
- **It does not show a winning model.** The first predictor is a smoothed outcome-frequency baseline, and the rating model has not been scored on real fixtures yet. Both exist as comparators, not as evidence of skill.
- **It does not give you data.** You supply your own provider key, Supabase project and (optionally) Telegram bot. No maintainer accounts or datasets are shared.

## What the evidence shows so far

The maintainer's earlier private system (a pick-and-confidence LLM pipeline, not this code) has a record from May to September 2026. Read [the baseline](docs/BASELINE.md) before quoting any number from it. In short:

- Over 4,295 settled pre-kickoff predictions it was right 42.5% of the time (95% interval 41.0–44.0). Always predicting a home win would have been right 44.0% of the time. **The record is not distinguishable from always predicting HOME.**
- Its stated confidence exceeded its hit rate in every confidence band, by about 19 points on average, and most of all for draws.
- Those numbers cannot be reproduced by readers because the underlying rows are private. The method can be re-run on your own data.

Accuracy, probability calibration and financial return are different measures. A model can pick many winners and still assign poor probabilities, or lose money after costs. This project reports prediction quality only and makes no statement about financial return.

## How it fits together

```
fixtures -> prediction-time features -> prediction (probabilities) -> final result -> evaluation -> optional report
```

In a deployed install the stages are scheduled independently and exchange state through your Supabase project, never by importing each other. `proracle.flow` is a single-process synthetic harness for development, not production orchestration. See [the architecture and prediction contract](docs/ARCHITECTURE.md).

## Try it in five minutes (no accounts)

Python 3.11 or newer is required. No packages and no credentials are needed:

```sh
python -m proracle.demo
python -m proracle.evaluate --training-results data/synthetic/training_results.jsonl --predictions data/synthetic/predictions.jsonl
python -m unittest discover -s tests -v
```

The demo and evaluator run on a handful of **fabricated** fixtures. The demo output is marked `"data_kind": "synthetic"`, and the numbers from both are a smoke test, not a performance estimate.

## Run it on your own accounts

Full setup, secret handling, scheduling, costs, limits, failure behaviour and teardown are in [docs/OPERATIONS.md](docs/OPERATIONS.md). The pieces, in order:

**1. Database.** Apply the checked-in migrations to your own disposable or dedicated Supabase project ([docs/DATABASE.md](docs/DATABASE.md)). Never point a trial run at a database you care about.

**2. Fixtures and results.** The runner needs an operator-owned BSD API token. Preview fetches a bounded window and writes nothing:

```sh
python -m proracle.self_hosted --dry-run
```

Set `BSD_API_TOKEN` in your local environment first. To write records, also set `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`, then:

```sh
python -m proracle.self_hosted --execute
```

`SUPABASE_SERVICE_KEY` is a server-side secret: never put it in browser code, source control, issues, pull requests or a shared `.env` file. The adapter keeps only the approved fixture fields and regulation-time final scores. Provider odds, raw responses and free-form error text are discarded. Use remains subject to your provider agreement and local law; commercial use of provider-derived data has not been cleared. See [the data policy](docs/DATA_POLICY.md).

**3. Retrospective report.** After results arrive, a read-only report scores one model's stored predictions against the final results in your database:

The report files contain your private data and stay on your machine. Create the local output directory before redirecting a report into it:

```sh
python -c "from pathlib import Path; Path('reports').mkdir(parents=True, exist_ok=True)"
```

```sh
python -m proracle.retrospective --from 2026-11-01 --until 2026-12-01 --model-name historical_frequency --export-dir reports/retrospective
```

Each run stores two model identities, so name the one to score. To compare the rating model with the baseline on the same fixtures:

```sh
python -m proracle.retrospective --from 2026-11-01 --model-name elo_davidson_recalibrated --reference-model-name historical_frequency
```

It scores the earliest prediction per fixture, trains the baseline only on results known before the cohort began, excludes void, cancelled and pending fixtures with stated reasons, and reports sample size, 95% intervals, slices and the difference from the baseline. `--export-dir` also writes JSONL the offline evaluator accepts. Real rows stay private (`reports/` is git-ignored); share aggregates only after a data-rights review. See [decision 0003](docs/decisions/0003-point-in-time-retrospective-cohorts.md).

**4. Telegram summary (optional).** Create a bot with BotFather, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, then preview and send:

```sh
python -m proracle.retrospective --from 2026-11-01 --until 2026-12-01 --model-name historical_frequency > reports/report.json
python -m proracle.telegram_report --report reports/report.json          # preview only
python -m proracle.telegram_report --report reports/report.json --send    # sends
```

The sender is standalone: a failure never changes predictions, results or reports, and error messages are fixed codes that never contain your token. See [decision 0004](docs/decisions/0004-optional-standalone-telegram-report.md).

## Contribute

A small, reproducible result is worth more than a large claim without a method.

- Start with [the research agenda](docs/RESEARCH_AGENDA.md) and [the evaluation protocol](docs/EVALUATION_PROTOCOL.md). Propose a measurable experiment (question, baseline, data permission, split, metric, acceptance rule) with the issue forms.
- Report null and negative results too. Use [the finding template](docs/SHARE_FINDINGS.md).
- Read [CONTRIBUTING.md](CONTRIBUTING.md) for the pull-request process, and [the roadmap](docs/ROADMAP.md) for open work.
- Do not submit provider responses, production reports, credentials or private data. Examples must be synthetic or cleared for redistribution.
- Pull requests and pushes to `main` run the unprivileged CI workflow: offline tests, a compile check, the synthetic demo, and a secret scan. It uses a read-only token and no secrets; it does not deploy or access production services. Run the test suite locally before you open a pull request.
- Report a vulnerability or an exposed credential privately, as described in [SECURITY.md](SECURITY.md). Conduct concerns follow [the code of conduct](CODE_OF_CONDUCT.md).

## Licence and data

- Code is licensed under the Apache License, Version 2.0 ([LICENSE](LICENSE)).
- Original documentation, reports and synthetic example data are licensed under Creative Commons Attribution 4.0 International ([LICENSE-DOCS.md](LICENSE-DOCS.md)). See [NOTICE](NOTICE) for the authorship and AI-assistance statement.
- Third-party data, provider-derived outputs, trademarks and logos are not covered by either licence and remain subject to their owners' terms.
