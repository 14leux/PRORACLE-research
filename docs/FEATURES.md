# Point-in-time match features (results only)

`proracle.features` computes a fixed set of match features from the results PRORACLE has already stored. It uses no provider field, no odds, no lineup, no standing and no news. Its inputs are exactly what the [rating model](RATING_MODEL.md) uses: team labels, kickoff, the local time a result became available, and the regulation-time final outcome ([decision 0001](decisions/0001-bsd-user-keyed-adapter.md)).

**Status:** implemented and tested offline. It is not wired into any model or prediction yet, and no feature has been shown to improve a prediction. Nothing here is evidence of real prediction skill. It exists so that a later model can be given precomputed, checkable numbers instead of raw strings, and so that every such number has a tested point-in-time rule.

## The leakage rule

For a prediction made at `as_of` for a fixture kicking off at `kickoff_at`:

1. `as_of` must be strictly before `kickoff_at`, otherwise the call raises.
2. Only results whose availability time is strictly before `as_of` **and** whose kickoff is strictly before `as_of` are used (`proracle.rating.visible_results`, the same function the rating model uses). A result recorded or backfilled later is never visible to an earlier prediction.
3. Input order never matters. The same inputs give the same numbers and the same digest.
4. Missing is never 0. A rest-day value is `None` when the team has no visible result. Counts always come with their sample size (`*_n`), so "0 wins from 0 games" is distinguishable from "0 wins from 5".

Results backfilled in bulk have an availability time equal to the backfill moment (decision 0001), so they are valid for forward-looking features and not for back-dated evaluation.

## What is computed (feature set v1)

Per team (prefix `home_` for the fixture's home team, `away_` for its away team):

| Feature | Meaning |
|---|---|
| `{p}_games_visible` | Visible results involving the team, any venue |
| `{p}_form_n / _wins / _draws / _losses / _points` | The team's last 5 visible results, any venue (3 points for a win, 1 for a draw) |
| `{p}_venue_form_n / _wins / _draws / _losses / _points` | The team's last 5 visible results at its own venue (home for the home team, away for the away team) |
| `{p}_rest_days` | Days from the team's most recent visible result's kickoff to **this fixture's kickoff** (not to `as_of`); `None` if none |
| `{p}_matches_7d / _14d / _30d` | Visible results with kickoff in `[kickoff − N days, kickoff)` |

Per fixture: `h2h_n / h2h_home_wins / h2h_draws / h2h_away_wins` (the two teams' last 5 visible meetings at either venue, seen from this fixture's home team), `hours_to_kickoff`, and `competition_cup_keyword` (1 if the competition name contains a cup word such as cup, copa, coppa, coupe, pokal, taça, beker; 0 if it does not; `None` if the name is unknown; a hint, not a classification).

36 features in all (`proracle.features.feature_names()`). The result is a `FeatureVector` carrying the version, `as_of`, the two normalized team keys, the entries, and a SHA-256 over those plus a digest of the visible history it was built from.

## Using it

```python
from proracle.features import FeatureHistory, compute_features

history = FeatureHistory(rating_results, as_of)           # build once per run
vector = compute_features(history, "Alpha", "Beta", kickoff_at, competition_name)
vector.as_dict()   # {"home_form_wins": 2, "home_rest_days": 5.0, "h2h_n": 3, ...}
```

## Limits

- **Outcomes only.** The store keeps the regulation-time outcome, not the score, so there are no goal features.
- **Team labels, not ids.** Teams are keyed on normalized labels (the rating model's rule), so two clubs sharing a label collide and one club under two labels splits.
- **Rest days are to the last visible result.** A fixture that was postponed or has no stored result is invisible, so a rest-day value can be too long.
- **The cup keyword is a name hint.** It misses cups whose names contain none of the listed words, and it needs a competition name; where the name is empty the feature is `None`.
- **A new feature, a changed window or a changed rule is a new version.** Bump `FEATURES_VERSION` in the same change; never edit a released version in place. A model that uses these features is a new model identity ([decision 0003](decisions/0003-point-in-time-retrospective-cohorts.md)).
