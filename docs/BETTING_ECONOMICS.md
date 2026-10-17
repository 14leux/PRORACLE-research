# Betting economics research track

**Status:** documentation only. This project contains no betting code, no price data and no strategy. This page defines how a separate research discussion should be run so that claims about money are as careful as claims about accuracy. It is not financial, tax or legal advice, and it is not encouragement to wager. Wagering carries a risk of losing money, and rules on age, licensing and taxation differ by place; check the ones that apply to you.

## Why this is separate from prediction

PRORACLE's prediction path never takes bookmaker prices as an input and never settles stakes. A good probability forecast does not by itself make money: returns depend on the price you can actually get, the size you can place, what you pay in fees and taxes, and how the market settles. Accuracy, probability calibration and financial return are different measures, and this track exists so the third is not smuggled in on the back of the first two. See [the baseline](BASELINE.md) for how little the project's accuracy record currently shows.

## Rules for any contribution to this track

1. **No net-return claim without a complete assumption ledger** (below). An unknown cost is recorded as unknown, and a figure that depends on it is withheld.
2. **No default tax or fee rate.** The project supplies none, for any country. Do not reuse a formula from another project or from earlier private work as if it were law.
3. **Paper analysis first.** Nothing here asks anyone to place a wager.
4. **Rights.** Price data belongs to its source. Do not add bookmaker or exchange prices, screenshots or scrapers to this repository unless you hold written permission that allows publication. Do not collect prices in breach of a site's terms.
5. **Pre-register.** State the question, data window, cost assumptions and acceptance metric before looking at the result, and publish null and negative results.

## Definitions

Use decimal odds `d` (the total returned per 1 staked, including the stake).

- **Implied probability** of one outcome: `1 / d`.
- **Overround (margin)** of a complete market: the sum of `1 / d` over all outcomes, minus 1.
- **Break-even probability:** the probability at which a single wager has zero expected net return. With net win `W` and net loss `L` per unit stake (after all fees and taxes, `L` negative), it is `|L| / (W + |L|)`. With no costs, `W = d - 1`, `L = -1`, and it reduces to `1 / d`.

### Worked example (hypothetical numbers, not any venue)

A three-way market quoted at 2.60 / 3.20 / 2.90 has implied probabilities 38.5% / 31.3% / 34.5%, which sum to 104.2%: a 4.2% overround. Removing it proportionally gives about 36.9% / 30.0% / 33.1%, which are *not* the market's "true" probabilities, only one normalisation.

Suppose a hypothetical 10% tax is charged on winnings only (an illustration, not any jurisdiction's rule). At `d = 2.60` the net win is `1.60 x 0.90 = 1.44`, so the break-even probability rises from 38.5% to `1 / (1 + 1.44) = 41.0%`. A forecast that beat the quoted price before the tax can fall below break-even after it.

## Stake sizing

Stake sizing is its own source of error. The Kelly criterion for a single wager with net odds `b` and win probability `p` is `f* = (b p - (1 - p)) / b`. It maximises long-run growth only if `p` is exactly right.

Illustration: at `b = 1.60` and a forecast `p = 0.42`, `f* = (0.672 - 0.58) / 1.60 = 5.75%` of bankroll. If the true probability is `0.38`, the expected net return per unit staked is `1.60 x 0.38 - 0.62 = -0.012`, negative, yet the formula still said to stake 5.75%. Overestimated probabilities cause over-staking, and the forecasts evaluated by this project carry the sampling uncertainty shown in their intervals. Studies must therefore:

- report the stake rule (flat, fractional Kelly, other) and why it was chosen;
- carry forecast uncertainty into the sizing, or show results across a range of assumed forecast error;
- report the drawdown path and the probability of ruin, not only the average.

## What an executable price means

A quoted price is executable only if, at the time of the decision, you could have placed the stake you assumed. Record all of the following for each observation:

- price, size available, and timestamp (to the second) with time zone;
- when the decision was made, relative to kickoff and to the quote;
- whether the quote was stale, suspended or withdrawn before fill;
- fill behaviour: full, partial, rejected, or repriced;
- stake limits and any account-level restrictions you actually met;
- the market definition and settlement rule (see below).

A price table taken after the fact, a closing line, or a screenshot without a timestamp is a descriptive observation, not an executable one.

## Settlement and market definitions

Two prices are comparable only if they settle the same event the same way. Check and record:

- regulation time only, or including extra time and penalties;
- treatment of postponed, abandoned and void fixtures, including voids that apply on one venue and not another;
- dead-heat and partial-refund rules;
- the competition and fixture identity (same match, same date, same participants);
- the data source used by each venue to settle, and when it settles.

A mismatch here can turn an apparent gain into a loss, or leave one leg void and the other live.

## Arbitrage

An arbitrage claim means: a set of simultaneous positions across the outcomes of one event whose *combined net return is non-negative in every outcome*, after every cost. A reciprocal-odds sum below 1 is a screening signal, not that claim.

Illustration (hypothetical): opposite outcomes of a two-way market priced 2.10 at one venue and 2.05 at another give `1/2.10 + 1/2.05 = 0.964`. Stakes split to equalise the payout return about 3.7% of the total stake gross, if both legs fill at those prices. A cost of about 3.6% or more of the winning payout removes it. One leg failing to fill, a price moving before the second leg, or a settlement mismatch leaves a single directional position that can lose.

A public arbitrage result must therefore show, for each opportunity:

1. simultaneous, timestamped quotes with sizes at compatible venues;
2. identical market and settlement rules;
3. every fee, commission, tax and conversion cost, with its source;
4. the fill outcome and the settled return, matched against the quoted plan in an after-the-fact audit.

Screening counts from past data, such as how often a reciprocal sum fell below 1, may be reported as screening counts only. They are not evidence of executable or net return.

## Assumption ledger

Every financial assumption used in a result goes in a table like this one. A result with an empty or "unknown" row in a column it depends on makes no net-return claim.

| Field | What to record |
|---|---|
| Assumption | e.g. tax on winnings, commission rate, stake limit |
| Jurisdiction | The place whose rule you applied |
| Currency | Including any conversion and its rate source |
| Venue / operator | Named, with the account type used |
| Product / market | The exact market definition |
| Value | The number, or "unknown" |
| Effective date | When the rule applied; rules change |
| Source | Link or document, and who checked it |
| Status | known / estimated / unknown |

## Report template

State, in order: the question and pre-stated hypothesis; the forecast source and its out-of-sample evidence; the price source, its rights and how it was captured; the full ledger; the stake rule; the period and sample size; the result with uncertainty, drawdown and ruin figures; slices and regressions; what you could not verify. Do not report an expected return derived from win rate or from retrospective accuracy.

## Questions this track could take up

Each would become an issue with a baseline, hypothesis, acceptance metric and regression guard:

- How large is the gap between break-even probability and forecast probability once fees and taxes are included, across venues and markets?
- How does the margin of a market vary by competition, time to kickoff and market depth?
- How often are quoted prices actually executable at the assumed size, and how fast do they go stale?
- How sensitive are stake-sizing rules to forecast error at this project's observed sample sizes?
- How often do equivalent markets settle differently across venues?
- How large do fixed and per-leg costs need to be to remove an apparent cross-venue difference?

## Related

[Research agenda](RESEARCH_AGENDA.md) | [Evaluation protocol](EVALUATION_PROTOCOL.md) | [Data policy](DATA_POLICY.md) | [Contributing](../CONTRIBUTING.md)
