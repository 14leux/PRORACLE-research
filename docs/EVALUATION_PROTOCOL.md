# Evaluation Protocol

This protocol is the shared starting point for comparing PRORACLE prediction changes. A future runnable implementation may refine the exact commands and cohort definitions; changes must be documented before comparing results.

The offline evaluator is `python -m proracle.evaluate`. It accepts a JSON Lines training-results file and a separate, later predictions file. Each training row contains `fixture_id`, timezone-aware `kickoff_at`, and `actual_outcome`; it may also include timezone-aware `available_at`, which cannot precede kickoff. Each evaluation row contains timezone-aware `prediction_at` and `probabilities` for `HOME_WIN`, `DRAW`, and `AWAY_WIN`. The tool rejects predictions recorded at or after kickoff and rejects any training result whose effective availability time is not earlier than every evaluation prediction. When `available_at` is missing, `kickoff_at` is used as a compatibility proxy; this does not prove when the result was actually published. See `data/synthetic/` for fabricated examples.

The evaluator comparison baseline uses add-one-smoothed outcome frequencies from the training file and holds those probabilities constant over the evaluation cohort. The B3 prediction-creation baseline uses the same smoothing rule but filters training outcomes by `available_at` (or the documented kickoff proxy) before each prediction time. The report includes accuracy, mean log loss, multiclass Brier score, ten-bin classwise calibration error and reliability bins, and a confusion matrix for both candidate and evaluator baseline. Calibration summaries are descriptive on small samples. When top probabilities tie, the predicted class follows the fixed order HOME_WIN, DRAW, AWAY_WIN. Synthetic scores demonstrate the tool only; they are not evidence about real match prediction.

## Retrospective cohorts from stored rows

`python -m proracle.retrospective` builds a cohort from the operator's own database (read-only) under [decision 0003](decisions/0003-point-in-time-retrospective-cohorts.md): earliest prediction per fixture for a single model identity; training results known strictly before the cohort's earliest prediction, as of that moment; current non-`FINAL` results excluded with their reason; and refusal when the model identity or result chain is ambiguous. The report adds sample size, 95% intervals, outcome/competition/month slices, exclusions, and a cohort digest. Re-exporting the cohort later and comparing digests shows whether any scored prediction or label changed. Fewer than 30 scored fixtures is descriptive only, and an interval spanning zero is not evidence of improvement.

## Required safeguards

1. **Define the prediction moment.** Use only information available at that time. Exclude post-kickoff data and any field updated using the match result.
2. **Split by time.** Train and tune on earlier periods. Reserve a later period for evaluation. Do not repeatedly tune against the final holdout.
3. **Use a baseline.** Compare with a simple baseline using the same fixtures and exclusions.
4. **Preserve the cohort.** Record fixture identifiers privately where needed, cohort dates, competitions, excluded fixtures, and reason for each exclusion. Public identifiers or row-level data require rights review.
5. **Measure probabilities.** Report log loss and Brier score alongside outcome accuracy; add calibration plots or grouped calibration measures when the sample supports them.
6. **Show uncertainty.** Include sample size and confidence intervals or a justified alternative. Avoid treating small subgroups as established findings.
7. **Report all slices.** Show home/draw/away and relevant competition/time slices, including regressions and null results.
8. **Make it reproducible.** Record code revision, model/provider and version where permitted, prompt version, configuration, source terms, and commands. Never publish credentials or restricted source rows.
9. **Verify result labels before scoring or making material claims.** Prefer the official competition/league or governing-body result record where available. Confirm disputed, corrected, or consequential labels against at least one independent, detailed source; a search snippet or search-result summary is not evidence. Record source URLs or identifiers, retrieval/finalization times, the selected value, and any correction/supersession. If reliable corroboration is unavailable, mark the label unresolved and exclude it from scored claims until resolved.

## Required experiment report

- Question and pre-stated hypothesis
- Baseline and candidate change
- Data source, permission basis, date range, and prediction-time cutoff
- Result-label source, retrieval/finalization time, corroboration, and correction history for the evaluated cohort
- Time split, cohort size, exclusions, and missing-data handling
- Metrics, uncertainty, subgroup results, and cost where relevant
- Code revision and steps for independent reproduction
- Limitations, regressions, and whether the result was expected

Do not infer expected financial return from win rate or retrospective model accuracy. A betting-return study, if separately approved, needs its own market, price, execution, stake, fees, tax, settlement, and bankroll protocol.
