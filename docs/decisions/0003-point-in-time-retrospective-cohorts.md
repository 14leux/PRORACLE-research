# Decision 0003: Score Retrospective Cohorts Point-in-Time

- **Status:** Accepted by the maintainer (B5 scope)
- **Date:** 2026-10-08

## Context

Predictions and result versions are append-only in the database. A retrospective report can quietly leak future knowledge in four ways: scoring a favorable rerun, using a label that was corrected after the prediction, leaving a later-voided label in the baseline, or mixing models in one number.

## Decision

- **Score the earliest prediction per fixture** for one model identity (provider, name, version, prompt version, input schema). Reruns are counted as exclusions, never chosen.
- **Training results are those known strictly before the cohort's earliest scored prediction**, using the result version that was current at that moment. A later correction cannot rewrite what a past baseline could have known.
- **Scoring labels are the current result version.** A fixture whose current version is not `FINAL` (`VOID`, `CANCELLED`, `ABANDONED`) is excluded with its reason. Corrections are resolved across all statuses first, so a voided result also leaves the training set.
- **Refuse ambiguity.** More than one model identity, a branching result chain, or a result available before kickoff stops the report instead of guessing.
- **Report with uncertainty.** Every report states sample size, 95% Wilson intervals for accuracy, seeded paired-bootstrap intervals for the loss difference against the baseline, outcome/competition/month slices (flagged under 30 fixtures), exclusions with reasons, and a cohort digest.

## Trade-off

Resolving the as-of version needs every result version, not just final rows, so retrospective reads fetch more rows. Real exported rows stay private (`reports/` is ignored); only aggregates may be shared, after data-rights review.
