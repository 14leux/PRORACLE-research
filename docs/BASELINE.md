# Baseline: what the earlier system's record shows

**Status:** observation as of 2026-10-10. This is a description of a past record, not a claim about how PRORACLE will perform. Read the limits below before quoting any number.

## Summary

- Over **4,295** settled, pre-kickoff predictions made between 2026-05-03 and 2026-09-19, the maintainer's earlier private system picked the correct outcome **42.5%** of the time (95% interval 41.0–44.0).
- Always predicting a home win on the same fixtures would have been right **44.0%** of the time. The paired difference is **-1.5 points** (95% interval -3.5 to +0.5). **The record is not distinguishable from always predicting HOME.** It does not show an advantage over that baseline, and it does not prove the earlier system was worse.
- The stated confidence was far too high: on average it exceeded the observed hit rate by about **19 points**, in every confidence band. The overstatement was largest for draws: draw picks at about 60 stated confidence hit 28.1%, against a draw base rate of 25.2%.
- Nothing here estimates financial return. No prices or stakes were used, and accuracy must not be converted into an expected return.

## What this is, and what it is not

- It is the pick-and-confidence record of the maintainer's earlier private system (called ORACLE), an LLM-based pipeline. That code is **not** in this repository, and this repository's `proracle` package has produced no real-data results yet.
- The underlying rows are private and are not published. Only the aggregates below are. **You cannot reproduce these numbers**, because you do not have the rows. The method is described so it can be checked and re-run on your own data with `python -m proracle.retrospective` (see the README).
- The earlier system recorded a pick and an integer confidence (0–100), not a probability for each of home, draw and away. **Log loss and Brier score cannot be computed from it** and are not reported.

## Cohort

| | |
|---|---|
| Source | The earlier private system's prediction and result tables (read-only aggregate queries) |
| Eligibility | Prediction stored **before kickoff**; final outcome is a settled home win, draw or away win (postponed and abandoned fixtures excluded, not counted as misses) |
| Cutoff | Predictions created before 2026-10-01 UTC. October fixtures were only partly settled; keeping just the settled ones would have favoured fast-settling fixtures |
| One prediction per fixture | Yes. The system stored a single prediction per fixture, so "earliest prediction" is that prediction |
| Windows (fixed in advance) | **Early:** 2026-05-03 to before 2026-08-03 (n=1,899). **Later:** 2026-08-03 to before 2026-10-01 (n=2,396; the last eligible prediction is from 2026-09-19). The boundary is a recorded change in the system's prompt regime |
| Fingerprint | md5 over the sorted prediction ids: `973e7bfeb66f524279d06606de5c6a1f` (all), `24faa2ea2a0c85351448a18626556955` (early), `90e666581844ef8c46022ac7587a3323` (later). The maintainer can re-derive the exact member set; readers cannot |
| Exclusions | 179 voided fixtures across all dates; October 2026 predictions (123 settled, more unsettled); predictions stored at or after kickoff; fixtures never settled |

## Results

### Overall accuracy

| Window | n | Accuracy (95% Wilson interval) | Mean stated confidence | Stated minus observed (points) |
|---|---:|---|---:|---:|
| Early | 1,899 | 41.6% (39.4–43.8) | 62.6 | +21.0 |
| Later | 2,396 | 43.2% (41.2–45.2) | 60.9 | +17.7 |
| All | 4,295 | 42.5% (41.0–44.0) | 61.7 | +19.2 |

### Against the always-HOME baseline

This is the only comparator computed. It is also the "learn from the early window, apply to the later window" baseline, because home win was the most frequent outcome in the early window. The difference is paired (same fixtures); intervals are normal approximations for the mean per-fixture difference.

| Window | n | Earlier system | Always HOME | Difference, points (95% interval) |
|---|---:|---:|---:|---|
| Early | 1,899 | 41.6% | 44.7% | -3.1 (-6.3 to +0.1) |
| Later | 2,396 | 43.2% | 43.4% | -0.2 (-2.8 to +2.3) |
| All | 4,295 | 42.5% | 44.0% | -1.5 (-3.5 to +0.5) |

Every interval includes zero. The data cannot separate the earlier system from always predicting HOME, in either window or overall. The point estimates favour the baseline.

### By predicted outcome (all windows)

| Pick | n | Hit rate (95% Wilson interval) | Mean stated confidence | Stated minus observed |
|---|---:|---|---:|---:|
| Home win | 1,677 | 53.5% (51.1–55.9) | 60.6 | +7.1 |
| Draw | 1,260 | 28.1% (25.7–30.6) | 59.8 | +31.7 |
| Away win | 1,358 | 42.3% (39.7–44.9) | 64.8 | +22.5 |

By window (hit rate, n, stated minus observed):

| Window | Home win picks | Draw picks | Away win picks |
|---|---|---|---|
| Early | 54.3% (n=628; +7.5) | 28.2% (n=625; +31.9) | 42.3% (n=646; +23.7) |
| Later | 53.0% (n=1,049; +6.8) | 28.0% (n=635; +31.5) | 42.3% (n=712; +21.4) |

Actual outcome rates in the cohort, and how often each outcome was picked (home / draw / away, %):

| Window | Actual outcome rate | Pick share |
|---|---|---|
| Early | 44.7 / 24.9 / 30.4 | 33.1 / 32.9 / 34.0 |
| Later | 43.4 / 25.5 / 31.1 | 43.8 / 26.5 / 29.7 |
| All | 44.0 / 25.2 / 30.8 | 39.0 / 29.3 / 31.6 |

Home picks were right about 53% of the time against a 44% home base rate; draw picks about 28% against a 25% draw base rate. Draws were picked in about 29% of cases. The draw overstatement was nearly the same in both windows.

### Calibration of stated confidence (all windows)

| Stated confidence | n | Hit rate | Mean stated | Stated minus observed |
|---|---:|---:|---:|---:|
| 00-49 | 28 | 39.3% | 43.9 | +4.6 |
| 50-59 | 1,426 | 39.2% | 57.5 | +18.3 |
| 60-69 | 2,403 | 42.6% | 62.3 | +19.7 |
| 70-79 | 426 | 53.0% | 72.6 | +19.6 |
| 80-100 | 12 | 50.0% | 85.9 | +35.9 |

The hit rate does rise with stated confidence in the 70–79 band, but stated confidence exceeds the hit rate in every band. The 80–100 band has only 12 cases and carries no information.

### Slices (exploratory)

Thirty league slices and six prediction-tag slices with at least 60 fixtures are shown in full, so no favourable subset is selected. The slices were defined after looking at the data. Seven of the 36 paired-difference intervals exclude zero (five favourable to the earlier system, two unfavourable), against about two expected by chance alone. **Treat none of them as findings.** They are leads for pre-registered tests on new data.

<details><summary>League slices (n ≥ 60)</summary>

| League | n | Earlier system | Always HOME | Difference, points (95% interval) |
|---|---:|---:|---:|---|
| Club Friendlies | 275 | 41.1% | 44.4% | -3.3 (-11.8 to +5.3) |
| MLS | 186 | 38.2% | 42.5% | -4.3 (-14.0 to +5.4) |
| Conference League | 175 | 38.9% | 46.3% | -7.4 (-16.6 to +1.7) |
| Brasileirao Serie B | 173 | 42.2% | 49.7% | -7.5 (-17.7 to +2.6) |
| USL Championship | 154 | 39.0% | 40.3% | -1.3 (-11.5 to +8.9) |
| J1 League | 113 | 43.4% | 46.0% | -2.7 (-14.8 to +9.5) |
| Chinese Super League | 111 | 36.0% | 43.2% | -7.2 (-20.6 to +6.2) |
| K League 1 | 97 | 38.1% | 34.0% | +4.1 (-8.7 to +16.9) |
| Allsvenskan | 95 | 36.8% | 42.1% | -5.3 (-18.5 to +8.0) |
| World Cup 2026 | 94 | 59.6% | 44.7% | +14.9 (+1.6 to +28.1) |
| Veikkausliiga | 92 | 40.2% | 43.5% | -3.3 (-17.3 to +10.8) |
| Europa League | 86 | 46.5% | 50.0% | -3.5 (-16.2 to +9.3) |
| Saudi Pro League | 86 | 61.6% | 40.7% | +20.9 (+6.8 to +35.1) |
| Botola Pro | 85 | 41.2% | 40.0% | +1.2 (-13.3 to +15.7) |
| Liga Profesional de Futbol | 84 | 42.9% | 46.4% | -3.6 (-12.6 to +5.5) |
| International Friendly Games | 84 | 45.2% | 46.4% | -1.2 (-13.4 to +11.0) |
| La Liga | 78 | 47.4% | 47.4% | +0.0 (-13.4 to +13.4) |
| Champions League | 76 | 52.6% | 55.3% | -2.6 (-17.3 to +12.0) |
| NWSL | 75 | 42.7% | 49.3% | -6.7 (-22.6 to +9.3) |
| Parva Liga | 75 | 45.3% | 44.0% | +1.3 (-13.8 to +16.4) |
| Championship | 74 | 35.1% | 40.5% | -5.4 (-20.0 to +9.1) |
| Superliga | 73 | 39.7% | 47.9% | -8.2 (-24.3 to +7.9) |
| Pro League | 72 | 61.1% | 44.4% | +16.7 (+1.2 to +32.2) |
| Segunda Division | 72 | 43.1% | 36.1% | +6.9 (-7.7 to +21.6) |
| Categoria Primera A | 71 | 40.8% | 33.8% | +7.0 (-7.8 to +21.9) |
| Eredivisie | 69 | 49.3% | 30.4% | +18.8 (+4.1 to +33.6) |
| Carabao Cup | 67 | 44.8% | 43.3% | +1.5 (-14.9 to +17.9) |
| Liga MX Apertura | 65 | 36.9% | 38.5% | -1.5 (-17.9 to +14.8) |
| FA Cup | 64 | 17.2% | 45.3% | -28.1 (-46.4 to -9.9) |
| Ligue 2 | 61 | 34.4% | 32.8% | +1.6 (-14.6 to +17.8) |

</details>

<details><summary>Prediction-tag slices (n ≥ 60)</summary>

| Tag (the earlier system's own fixture categories) | n | Earlier system | Always HOME | Difference, points (95% interval) |
|---|---:|---:|---:|---|
| standard | 3278 | 43.3% | 44.1% | -0.8 (-3.0 to +1.4) |
| cup_rotation | 375 | 38.9% | 46.1% | -7.2 (-14.8 to +0.4) |
| relegation_six_pointer | 204 | 29.9% | 43.1% | -13.2 (-24.1 to -2.4) |
| post_european | 182 | 41.8% | 45.1% | -3.3 (-13.1 to +6.5) |
| derby | 98 | 43.9% | 43.9% | +0.0 (-14.5 to +14.5) |
| dead_rubber | 65 | 46.2% | 27.7% | +18.5 (+1.9 to +35.0) |

</details>

## Label check

Scoring depends on the recorded final outcomes being right, so a deterministic sample was checked by hand.

- **Sample:** the first 30 cohort predictions ordered by `md5(prediction id)`, drawn on 2026-10-10. Sample fingerprint (md5 of the sorted per-prediction md5 keys): `56ebe7065b575e77a0144d86ebb44af9`.
- **Method:** for each fixture the stored outcome was compared with the final score on independent detailed match pages (club or competition reports, league pages and results services). A search-result snippet alone did not count.

| Result | Count |
|---|---:|
| Stored outcome confirmed by two or more independent sources | 24 |
| Stored outcome confirmed by one source only | 4 |
| No independent result found (unresolved; not counted as correct) | 1 |
| Definition mismatch (see below) | 1 |
| Stored outcome contradicted by the match result | 0 |

- **Definition mismatch:** a knockout play-off tie that was level after 90 minutes and decided in extra time was stored as a home win. The earlier system had no written rule that outcomes are 90-minute (regulation-time) results, so in tied knockout matches its label can differ from the 90-minute result. The candidate adapter in this repository records regulation-time scores only. The size of this effect on the numbers above is unknown; it can only affect knockout-style fixtures.
- **What the check can and cannot say:** 0 contradictions among 29 resolved fixtures bounds a plain label-error rate at roughly 12% (95% upper limit) and no tighter. It does not show the labels are error-free.

## Limits

- **One deployment, one period.** About five months of one system, with eight stored model and prompt versions mixed within the windows. Results do not transfer to other systems or seasons.
- **No probabilities.** Pick and integer confidence only. Calibration here means "stated confidence versus hit rate", not probability calibration.
- **Selection.** Fixtures that never received a result were not scored (about 60 pre-kickoff predictions across July and August). Void fixtures were excluded. If missing results are not random, accuracy could shift.
- **Mixed fixture types.** Club friendlies, domestic leagues, cups and tournaments are pooled. Small slices are noisy.
- **Multiple comparisons.** Thirty-six slices were inspected; some will look unusual by chance.
- **Label definition.** See the label check.
- **Not a return study.** No prices, stakes, fees or taxes are involved, and none of this supports a statement about financial return.

## Reproduce the method

The synthetic path needs no accounts and no data rights. It runs the same evaluation code on six fabricated fixtures. Its output is a smoke test, not a performance estimate:

```sh
python -m proracle.evaluate --training-results data/synthetic/training_results.jsonl --predictions data/synthetic/predictions.jsonl
```

To produce the equivalent report on your own predictions and results, with 95% intervals and the baseline comparison:

```sh
python -m proracle.retrospective --from 2026-11-01 --until 2026-12-01
```

State the cohort dates, exclusions and sample size when you share a result, as in [the evaluation protocol](EVALUATION_PROTOCOL.md) and [the finding template](SHARE_FINDINGS.md).

## What would change this conclusion

A later, pre-registered cohort in which a documented method beats the always-HOME baseline with an interval that excludes zero, scored on probabilities (log loss and Brier score), with labels verified under the protocol. Until then the honest reading is that this record shows no demonstrated advantage over the simplest baseline.
