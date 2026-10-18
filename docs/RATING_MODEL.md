# Rating model (statistical prior)

`proracle.rating` is PRORACLE's first model that tries to predict rather than to count. It is a walk-forward Elo rating with a Davidson draw term, shrinkage toward the base rate, and a small recalibration layer. It uses no language model, no bookmaker odds and no provider field beyond the [decision 0001](decisions/0001-bsd-user-keyed-adapter.md) allowlist. It costs nothing to run and needs only the standard library.

It has two jobs. It is the statistical prior that any later model can build on, and it is the honest bar: an AI predictor has to beat this, on the same fixtures, before anyone calls it useful ([decision 0005](decisions/0005-ai-predictor-evidence-bar.md)).

**Status:** implemented and tested offline on fabricated data only. No real fixture has been scored. Nothing here is evidence of real prediction skill.

## Model identity

| Field | Value |
|---|---|
| `model.provider` | `proracle` |
| `model.name` | `elo_davidson_recalibrated` |
| `model.version` | `1` |
| `prompt_version` | `not_applicable` |
| `input_schema_version` | `results-v1` |
| `input_snapshot_hash` | SHA-256 over the frozen constants, the visible result history (fixture id, normalized teams, kickoff, availability, outcome), the two normalized team keys, the prediction time and the fitted recalibration |

`proracle.self_hosted --execute` writes one prediction under this identity and one under the frequency baseline (`historical_frequency@1`) for every eligible fixture, at the same prediction time in the same run.

## How a prediction is made

For a fixture predicted at time `T`:

1. **Visible history.** Take every current `FINAL` result whose availability time is strictly before `T` and whose kickoff is strictly before `T`. Availability is the local first-seen time (decision 0001). Corrections and voids are resolved first, exactly as for the frequency baseline. Nothing at or after `T` is used anywhere below.
2. **Ratings.** Start every team at 1500. Process the visible results in kickoff order (fixture id breaks ties). Each result moves the two ratings by `K * (score - expected)`, where `score` is 1, 0.5 or 0 for a home win, draw or away win, and `expected = 1 / (1 + 10^(-(R_home + 70 - R_away) / 400))`.
3. **Davidson probabilities.** With `d` the add-one smoothed draw rate of the visible history, `nu = 2d / (1 - d)`, and `x = 10^((R_home + 70 - R_away) / 800)`: `P(home) = x / (x + 1/x + nu)`, `P(draw) = nu / (...)`, `P(away) = (1/x) / (...)`. Two teams level after home advantage draw at exactly `d`.
4. **Shrinkage.** Let `n` be the number of visible results of whichever team has fewer. The rating probabilities get weight `n / (n + 5)` and the add-one base rate of the visible history gets the rest. A team with no history gets exactly the frequency baseline.
5. **Recalibration.** `q_k ∝ p_k^a * exp(b_k)` with `b_home = 0`. The parameters `(a, b_draw, b_away)` are fitted by Newton steps on summed log loss plus a ridge penalty `10/2 * ||theta - identity||^2`. The fit uses the *prequential* probabilities of the visible history: each result's probability is computed from the state just before that result was processed, so the layer learns only from out-of-sample rating probabilities. Below 300 visible results it is the identity. The slope is clamped to [0.25, 2] and each bias to [-1, 1].
6. **Output.** Each probability is floored at 1e-6 and the vector renormalized. The contract validator then checks three finite values summing to 1 within 1e-6.

The calculation is deterministic and independent of input order. The cycle builds the state once per run, because every fixture in a run shares one prediction time.

## Frozen constants

Frozen on 2026-10-18, before any PRORACLE-cohort prediction from this model was generated or scored, in one block at the top of `proracle/rating.py`:

| Constant | Value | Origin |
|---|---|---|
| Initial rating | 1500 | the earlier private project's frozen B3 rating baseline |
| K | 20 | B3 |
| Home advantage | 70 Elo points, one value for every fixture and competition | B3 |
| Draw term | Davidson, `nu` from the earlier add-one draw rate | B3 |
| Shrinkage | `n / (n + 5)`, `n` = fewer games of the two teams | chosen a priori; 5 matches B3's "rating ready" threshold |
| Recalibration minimum | 300 visible results, else identity | chosen a priori |
| Recalibration ridge | 10 | chosen a priori |
| Recalibration bounds | slope [0.25, 2], biases [-1, 1], 50 iterations | chosen a priori |
| Probability floor | 1e-6 | chosen a priori |

None of these were tuned on PRORACLE data. Changing any value creates a new model identity: bump `RATING_MODEL_VERSION` in the same change, and never edit a released version in place ([decision 0003](decisions/0003-point-in-time-retrospective-cohorts.md)). This is a reimplementation of B3 from its definition, not a copy of its code. B3 processed results in availability order. This model processes them in kickoff order, because a PRORACLE backfill marks every result available at the same instant.

## Inputs and rights

The inputs are the home and away team labels, the kickoff time, the result availability time and the regulation-time 1X2 outcome. All of these are already on the decision 0001 allowlist. The model reads no odds, forecasts, statistics, lineups or other provider fields, and stores no raw responses. Its predictions are provider-derived outputs and fall under the same installation-local retention and no-redistribution rules as everything else in your database ([data policy](DATA_POLICY.md)).

## Limits

- **Team-name keys.** PRORACLE does not store stable provider team ids, so ratings are keyed on normalized labels (casefold, punctuation removed, whitespace collapsed). Two different clubs with the same label, such as same-named clubs in different countries or a senior side and a reserve or women's side labelled alike, **share one rating**. A club whose label changes **splits** into two. Both bias predictions without failing. A fixture whose two labels normalize to the same key is refused. The fix is a separate allowlist decision to store provider team ids.
- **Cups and cross-league ties.** There is one rating pool and one home advantage. Ratings from different leagues are not anchored to each other, so cup ties between leagues are poorly priced.
- **Thin history.** With the default 30-day history most teams have few games, so shrinkage keeps predictions close to the base rate. A longer `PRORACLE_HISTORY_DAYS` backfill helps. A backfill's results all become available at the backfill moment, which makes them valid for forward evaluation only.
- **Label errors.** A wrong regulation-time label moves two ratings permanently, until a correction arrives and the next run rebuilds the state.
- **No failure events for this identity yet.** `proracle.prediction_failures` has no model-identity column. A failure event written for the rating model would be indistinguishable from a frequency-baseline failure for the same fixture. Rating failures are therefore counted only in the run summary (`rating_failures`) and never written as events. Coverage by reason for this identity needs a migration that adds nullable `model_provider`, `model_name` and `model_version` columns to `prediction_failures`, plus a matching contract change.
- **Isolation.** Any failure in the rating path (history read, state build, record validation or insert) is counted and skipped. It never stops, delays past its own work, or changes the baseline prediction.

## How it will be scored

```sh
python -m proracle.retrospective --model-name elo_davidson_recalibrated \
  --reference-model-name historical_frequency --from <protocol start date>
```

The paired comparison builds each identity's cohort under decision 0003, with the earliest prediction per fixture per identity. It compares only fixtures scored for both, counts unpaired fixtures, and reports the seeded paired-bootstrap difference in log loss and Brier score, DRAW calibration gaps, actual-outcome slices and median lead time. The bar this model must clear before it replaces the frequency baseline as the reported default is set in [decision 0005](decisions/0005-ai-predictor-evidence-bar.md): on its first 1,200 prospective settled fixtures after the protocol start date, the upper bound of the 95% interval for mean paired log loss (rating minus frequency) must be below 0.

The synthetic backtests in `tests/test_rating.py` and `tests/test_rating_retrospective.py` check the plumbing, the absence of leakage and unit-sum output. Their fabricated league is built so that ratings should win, and their scores say nothing about real football.
