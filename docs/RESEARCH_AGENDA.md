# Research Agenda

PRORACLE invites contributors to improve prediction quality through reproducible experiments. The agenda records questions to investigate; it does not claim the project has solved them.

## Prediction and calibration

- Compare the current model with simple majority-class, historical-frequency, and rating-based baselines.
- Measure log loss, Brier score, calibration error, and class-specific accuracy. Report confidence intervals or another uncertainty estimate.
- Test whether changes help across leagues, time periods, and home/draw/away classes rather than only on one favorable slice.
- Investigate draw errors and probability calibration without tuning against the final evaluation period.

## Data quality and coverage

- Measure missingness, freshness, duplicate fixtures, team-name matching, and result-label quality.
- Establish a result-label verification workflow: retain source provenance and timing, prefer authoritative competition records, independently corroborate disputed or corrected labels, and keep unresolved cases out of scored findings. Search snippets alone are not sufficient evidence.
- Quantify how each data field affects coverage and performance before adding it.
- Test robustness to delayed, missing, or contradictory source records.
- Document source terms, required attribution, geographic limits, and whether derived outputs may be published.

## Evaluation design

- Prevent future information from entering earlier predictions.
- Prefer chronological train, validation, and test partitions; keep the final test period untouched during iteration.
- Record all exclusions, settlement definitions, and model or prompt versions.
- Publish both improvements and null or negative results.

## Separate betting-economics study

Betting economics is a separate research discussion and is not part of the prediction feature pipeline. Any future study should begin as paper analysis and distinguish theoretical price differences from executable opportunities. It should account for contemporaneous available prices and sizes, equivalent market and settlement rules, commission, locally applicable taxes, fees, limits, liquidity, stale quotes, partial fills, voids, and drawdown. Label every financial assumption with its jurisdiction, currency, venue/operator, product or market, effective date, and evidence source. Contributors must check the laws and operator terms applicable to their own location and use case; the project supplies no global or Kenya-specific default rate or formula. Mark unknown costs as unknown and withhold net-return claims until resolved. A reciprocal-odds sum below one is a screening signal, not proof of realizable profit. The full ground rules, definitions, worked examples and assumption ledger are in [the betting-economics track](BETTING_ECONOMICS.md).

## Propose work

Open a discussion for an idea, then use an issue or pull request for a testable experiment. State the hypothesis, baseline, data provenance, evaluation split, metric, sample size, uncertainty, and acceptance criteria. See [Contributing](../CONTRIBUTING.md).
