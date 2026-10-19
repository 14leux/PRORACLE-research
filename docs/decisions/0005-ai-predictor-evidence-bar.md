# Decision 0005: Pre-registered Evidence Bar for an AI Predictor and for the Rating Model

- **Status:** Accepted as a pre-registration. Two fields below are deliberately left open and must be filled, in a later commit and before the first scored generation, by the maintainer: the protocol start date and the AI model identity.
- **Date:** 2026-10-18

## Context

PRORACLE's earlier private predecessor looked good mainly against itself and against a rating model that had too little history. Its only strong evidence for a language model was one retrospective replay, and its prospective test ended before its single planned analysis. PRORACLE must not repeat that. Any claim that an AI predictor adds information has to clear a bar written down before the first scored prediction, judged against an honest statistical prior on the same fixtures, with one analysis.

The statistical prior is the rating model in [docs/RATING_MODEL.md](../RATING_MODEL.md) (`proracle/elo_davidson_recalibrated@1`). It gets its own bar against the add-one frequency baseline.

## Fields to be frozen by the maintainer

| Field | Value |
|---|---|
| Protocol start date (UTC) | **TO BE FILLED BY THE MAINTAINER** before the first scored generation. Only predictions made at or after this instant count. |
| AI model identity under test | **TO BE FILLED BY THE MAINTAINER**: provider, a dated model id (not a moving alias), prompt file and hash, input schema version, and the model's stated knowledge cutoff. |

Filling these fields is the only permitted edit to this record before the analysis. Any other change voids the pre-registration and needs a new decision record.

## Decision

### Bar for the rating model (Shape 1)

- **Candidate:** `proracle/elo_davidson_recalibrated@1`. **Reference:** `proracle/historical_frequency@1`.
- **Cohort:** the first 1,200 fixtures, in order of the rating prediction's time, that have a current `FINAL` result and a valid pre-kickoff prediction from both identities made at or after the protocol start date. Earliest prediction per fixture per identity ([decision 0003](0003-point-in-time-retrospective-cohorts.md)). Both identities are written in the same scheduled run.
- **Test:** mean paired log loss, rating minus frequency, with the 95% seeded paired bootstrap of `proracle.retrospective.paired_bootstrap_interval` (seed 20261008, 2,000 resamples), computed by `build_paired_comparison`.
- **PASS:** the interval's upper bound is below 0. Only then may the rating model replace the frequency predictor as the reported default. Otherwise the frequency baseline stays the default and the result is published either way.

### Bar for adopting an AI predictor (Shape 2 or Shape 3)

- **Baseline to beat:** the rating model above (rating plus recalibration), on the same fixtures, with the earliest prediction per identity and both generated at the same scheduled run. The AI's prediction moment is fixed and disclosed. If it runs later than the baseline (for example after lineups appear), the information gap is reported as a limit.
- **Cohort:** the first 1,200 fixtures with a settled `FINAL` result whose AI prediction was valid and pre-kickoff, starting at the protocol start date. Everything earlier is tuning data and is never scored.
- **Looks:** one analysis at 1,200. An interim look at 600 checks validity, coverage and cost only. Accuracy, loss and calibration are not computed or examined at the interim.
- **Primary metric:** mean paired log loss, candidate minus rating model, with the 95% seeded paired bootstrap from the existing retrospective helper.
- **PASS requires all of:**
  1. interval upper bound below 0 **and** point estimate at or below -0.015;
  2. a better (lower) mean Brier score than the rating model;
  3. an absolute DRAW calibration gap (mean stated draw probability minus observed draw rate) no larger than the rating model's;
  4. no actual-outcome slice (home win, draw, away win) with n of at least 100 worse than the rating model by more than 0.05 mean log loss;
  5. validity at least 95% and coverage at least 80% of eligible fixtures, with every miss counted by reason;
  6. a placebo arm (the same model given another fixture's context, on a seeded 10% of fixtures) scoring worse than the real arm;
  7. every scored fixture kicking off after the model's stated knowledge cutoff.
- **Below the bar:** **PROMISING** (some but not all conditions met) means no adoption, and continuing needs a new pre-registration with a new, later cohort. **FAIL** means stop and publish the null result ([docs/SHARE_FINDINGS.md](../SHARE_FINDINGS.md)).

### Knowledge cutoff

A language model may have seen the results of matches played before its training cutoff. **Retrospective tests of a language model on fixtures before its stated knowledge cutoff are invalid**, and do not count as evidence for or against it. Every AI test is prospective, or uses only fixtures after the stated cutoff. A model whose provider states no cutoff is tested prospectively only.

## Trade-offs

A fixed cohort of 1,200 can detect a paired log-loss effect of roughly 0.02 at 80% power if the paired spread is close to the earlier private estimate. A real but smaller gain could be missed, and that cost is accepted in exchange for a single, uncherry-picked analysis. Requiring both identities on the same fixtures excludes fixtures where either failed, so coverage is reported separately and gates the result. The -0.015 floor is stricter than "the interval excludes zero", because the earlier private evidence suggested any real gain would be small and fragile.

## Revisit when

The maintainer fills the open fields (the only allowed edit); a migration adds model identity to failure events (coverage by reason can then be counted per identity in the database); or the rating model's version changes, which needs a new bar.
