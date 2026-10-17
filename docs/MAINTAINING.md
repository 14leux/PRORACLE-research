# Maintaining PRORACLE

This page is for maintainers. It records who reviews what, what CI may and may not do, how production is kept out of this repository, and the repository settings to apply and verify when the project is published. The settings were applied when the repository was published; the checklist below records what is verified and what is still open.

## Ownership

The maintainer owns the repository and is the default code owner (`.github/CODEOWNERS`). Changes that can affect trust, evaluation or claims have named owners in that file. CODEOWNERS review is advisory until branch rules require it.

## Review rules

| Change | Required before merge |
|---|---|
| Anything under `.github/` (workflows, templates, CODEOWNERS, dependabot) | Maintainer review. A workflow change is read line by line for triggers, permissions, secrets and pinned actions |
| New dependency, or a change to `tests/test_stdlib_only.py` | Written reason, licence check and dependency notice in the same pull request |
| Database migration under `supabase/` | Maintainer review and a run of the blank-database verifier; grants and row-level security re-checked |
| Prediction contract or evaluator change (`proracle/contracts.py`, `evaluate.py`, `retrospective.py`) | Maintainer review; tests prove temporal-leakage checks still fail when the guard is removed |
| Model or prompt change | A new versioned file (never overwrite a released prompt); comparison with a fixed baseline on a frozen cohort that tuning could not see; sample size and intervals; regressions listed |
| Any claim about accuracy, calibration or return | Evidence and limits in the same pull request, and a row in the maintainer's rights ledger for any new figure. Synthetic results demonstrate tooling only |
| Wording that implies profit, a winning strategy or a guarantee is not accepted | The release gate's claims guard runs on every candidate commit, and a reviewer checks the same |

## What CI may do

`.github/workflows/ci.yml` runs on `pull_request` and on pushes to `main`, with a read-only token and no secrets. It runs the offline tests on two Python versions, a compile check, the synthetic demo, and a Gitleaks scan with a pinned, checksum-verified binary.

Rules for any workflow in this repository:

- Triggers are only `pull_request`, `push` and, if ever needed, `schedule` or `workflow_dispatch`. Never `pull_request_target` or `workflow_run`, which can run untrusted code with trusted context.
- `permissions` is declared at the top and grants nothing beyond `contents: read` unless a maintainer has recorded why.
- No `secrets.*`, no `environment:`, no self-hosted runners, no deployment steps.
- Every third-party action is pinned to a full commit SHA, with the version in a comment. Dependabot proposes updates; read the diff before merging.
- Expressions that come from the event (titles, branch names, bodies) are never interpolated into `run:` scripts.
- Every job has a timeout, and the workflow cancels superseded runs.

The release gate in the maintainer's private tooling lints workflows for these rules.

## Production stays out of this repository

Decision: scheduled production runs live in a **private runtime that pulls a reviewed release tag**, not in a workflow here. The repository therefore holds no production secrets, no deployment workflow and no environment secrets, and a pull request or a merged change cannot reach them from GitHub.

Design of the private runtime (no code is shipped here):

- It checks out a specific tag that the maintainer has reviewed and that points to a commit on `main`, and refuses to run anything else.
- Credentials live only in that runtime's environment or secret store.
- One run at a time (a lock), a hard per-run cost cap for any paid call, and an alert when a run fails or does not start.
- Failures produce explicit failure records and never a partial prediction.

## Budget and concurrency controls

- CI: 10-minute job timeouts, superseded runs cancelled, two Python versions only.
- Production: a single concurrent run, a documented schedule, a cost cap per run, and a monthly budget alert on every paid account.
- No scheduled job in this repository: nothing here should consume quota unattended.

## Repository settings checklist (apply and verify at publication)

Applied on 2026-10-17, when the repository was published. Read each setting back from the repository's Settings page or API after any change; one item remains open, below.

- [x] Default branch protection (applied 2026-10-17 as a repository ruleset, no bypass actors): a pull request is required, the `tests (Python 3.11)`, `tests (Python 3.13)` and `secret scan` checks must pass, force pushes and deletion are blocked. No independent approval is required, so a merged pull request is not independent review.
- [x] Code-owner review: deliberately not required. GitHub does not let an author approve their own pull request, and this is a single-maintainer project; `.github/CODEOWNERS` still requests the owner as reviewer.
- [x] Actions (applied 2026-10-17): default workflow token read-only, Actions cannot approve pull requests, only GitHub-owned actions are allowed, and full-SHA pinning is required.
- [x] Fork pull requests: approval is required to run workflows from all outside contributors (applied 2026-10-17).
- [x] Secret scanning and push protection are on (applied 2026-10-17).
- [x] Private vulnerability reporting is on (applied 2026-10-17), as a second route beside the address in `SECURITY.md`.
- [x] Dependabot alerts and security updates are on, and `.github/dependabot.yml` keeps the pinned actions current.
- [x] Anonymous view verified 2026-10-17: Apache-2.0 shown, default branch `main`, a single root commit with no inherited history, no private identifiers.
- [ ] Open a test pull request from a fork with no secrets and confirm the workflow runs with a read-only token and sees no secrets. Not done: the maintainer has one GitHub account, so this needs a second account or a first outside contributor.

## Releases

Tag a release only from a commit on `main` that passed CI and that the maintainer has reviewed. The private runtime pulls tags, not branch heads.
