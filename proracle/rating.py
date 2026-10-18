"""Walk-forward Elo-Davidson outcome rating with shrinkage and a recalibration layer.

This is PRORACLE's statistical prior (Shape 1 in the design notes): a calibrated rating model
with no language model, no odds and no provider field beyond the decision-0001 allowlist
(fixture team labels, kickoff and regulation-time final outcomes). See docs/RATING_MODEL.md.

Everything here is pure and deterministic. Callers pass already-loaded results; for a
prediction made at time ``T`` only results whose availability time is strictly before ``T``
and whose kickoff is strictly before ``T`` are used, both for the ratings and for the
recalibration fit. Input order never matters.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from proracle.contracts import OUTCOMES, PredictionRecord, parse_prediction_record

# --- Frozen constants: rating model v1 ----------------------------------------------------
# Frozen on 2026-10-18, before any PRORACLE-cohort prediction from this model was generated
# or scored. K, home advantage and the Davidson draw rule reproduce the earlier private
# project's frozen "B3" rating baseline; shrinkage and recalibration constants were chosen
# a priori, not tuned on PRORACLE data. Changing any value is a new model identity: bump
# RATING_MODEL_VERSION in the same change and never edit a released version in place.
RATING_MODEL_PROVIDER = "proracle"
RATING_MODEL_NAME = "elo_davidson_recalibrated"
RATING_MODEL_VERSION = "1"
RATING_INPUT_SCHEMA_VERSION = "results-v1"
RATING_PROMPT_VERSION = "not_applicable"

INITIAL_RATING = 1500.0  # every team's rating before its first visible result
K_FACTOR = 20.0  # Elo step size per result
HOME_ADVANTAGE = 70.0  # Elo points added to the home side, one value for every fixture
ELO_SCALE = 400.0  # base-10 logistic scale
SHRINKAGE_GAMES = 5.0  # weight on the rating = n / (n + 5), n = fewer games of the two teams
RECAL_MIN_RESULTS = 300  # fewer visible results than this -> identity recalibration
RECAL_L2 = 10.0  # ridge penalty pulling the recalibration toward identity
RECAL_MAX_ITERATIONS = 50
RECAL_SLOPE_BOUNDS = (0.25, 2.0)
RECAL_BIAS_BOUND = 1.0
PROBABILITY_FLOOR = 1e-6

FROZEN_CONSTANTS = {
    "model": f"{RATING_MODEL_PROVIDER}/{RATING_MODEL_NAME}@{RATING_MODEL_VERSION}",
    "initial_rating": INITIAL_RATING,
    "k_factor": K_FACTOR,
    "home_advantage": HOME_ADVANTAGE,
    "elo_scale": ELO_SCALE,
    "shrinkage_games": SHRINKAGE_GAMES,
    "recal_min_results": RECAL_MIN_RESULTS,
    "recal_l2": RECAL_L2,
    "recal_max_iterations": RECAL_MAX_ITERATIONS,
    "recal_slope_bounds": list(RECAL_SLOPE_BOUNDS),
    "recal_bias_bound": RECAL_BIAS_BOUND,
    "probability_floor": PROBABILITY_FLOOR,
}
# ------------------------------------------------------------------------------------------

_SCORE = {"HOME_WIN": 1.0, "DRAW": 0.5, "AWAY_WIN": 0.0}
IDENTITY_CALIBRATION = (1.0, 0.0, 0.0)  # (slope, DRAW bias, AWAY_WIN bias); HOME_WIN bias = 0


@dataclass(frozen=True)
class RatingResult:
    """One current FINAL result with the fixture labels a rating needs."""

    fixture_id: str
    home_team: str
    away_team: str
    kickoff_at: datetime
    available_at: datetime
    actual_outcome: str


@dataclass(frozen=True)
class RatingState:
    """Everything a prediction at ``as_of`` may use; built only from strictly earlier results."""

    as_of: datetime
    ratings: dict[str, float]
    games: dict[str, int]
    base_rate: dict[str, float]
    draw_rate: float
    calibration: tuple[float, float, float]
    results_used: int
    history_sha256: str


def normalize_team_name(name: object) -> str:
    """Rating key: casefold, drop punctuation, collapse whitespace.

    Stable provider team ids are not stored, so ratings are keyed on labels. Two different
    clubs with the same label (or the same club under two labels) collide or split; see
    docs/RATING_MODEL.md for the risk.
    """
    if not isinstance(name, str):
        raise ValueError("team name must be a string")
    text = "".join(
        char for char in name.casefold() if not unicodedata.category(char).startswith("P")
    )
    key = " ".join(text.split())
    if not key:
        raise ValueError("team name is empty after normalization")
    return key


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def visible_results(results: Iterable[RatingResult], as_of: datetime) -> list[RatingResult]:
    """Results usable at ``as_of``: available strictly earlier and kicked off strictly earlier.

    Returned in kickoff order (fixture id breaks ties) so the rating sweep is deterministic and
    follows match order, not the order a backfill happened to record results.
    """
    cutoff = _utc(as_of, "as_of")
    usable: list[RatingResult] = []
    for result in results:
        if result.actual_outcome not in OUTCOMES:
            raise ValueError("rating results must have a 1X2 outcome")
        kickoff = _utc(result.kickoff_at, "kickoff_at")
        available = _utc(result.available_at, "available_at")
        if available < kickoff:
            raise ValueError("a result cannot be available before kickoff")
        if available < cutoff and kickoff < cutoff:
            usable.append(result)
    usable.sort(key=lambda item: (_utc(item.kickoff_at, "kickoff_at"), item.fixture_id))
    return usable


def _smoothed(counts: Counter[str]) -> dict[str, float]:
    total = sum(counts[outcome] for outcome in OUTCOMES) + len(OUTCOMES)
    return {outcome: (counts[outcome] + 1) / total for outcome in OUTCOMES}


def davidson_probabilities(
    home_rating: float, away_rating: float, draw_rate: float
) -> dict[str, float]:
    """Davidson 1X2 model; ``nu`` makes two equal ratings (after home advantage) draw at ``draw_rate``."""
    if not 0.0 < draw_rate < 1.0:
        raise ValueError("draw_rate must be strictly between 0 and 1")
    nu = 2.0 * draw_rate / (1.0 - draw_rate)
    diff = (home_rating + HOME_ADVANTAGE - away_rating) / ELO_SCALE
    # Divide through by sqrt(pi_home * pi_away) so only the rating difference matters.
    half = 10.0 ** (diff / 2.0)
    home, away, tie = half, 1.0 / half, nu
    total = home + away + tie
    return {"HOME_WIN": home / total, "DRAW": tie / total, "AWAY_WIN": away / total}


def _elo_update(home_rating: float, away_rating: float, outcome: str) -> tuple[float, float]:
    expected = 1.0 / (1.0 + 10.0 ** (-(home_rating + HOME_ADVANTAGE - away_rating) / ELO_SCALE))
    delta = K_FACTOR * (_SCORE[outcome] - expected)
    return home_rating + delta, away_rating - delta


def _shrunk(
    ratings: dict[str, float],
    games: dict[str, int],
    home: str,
    away: str,
    base_rate: dict[str, float],
) -> dict[str, float]:
    """Rating probabilities pulled toward the base rate when either team has little history."""
    elo = davidson_probabilities(
        ratings.get(home, INITIAL_RATING), ratings.get(away, INITIAL_RATING), base_rate["DRAW"]
    )
    evidence = min(games.get(home, 0), games.get(away, 0))
    weight = evidence / (evidence + SHRINKAGE_GAMES)
    return {
        outcome: weight * elo[outcome] + (1.0 - weight) * base_rate[outcome]
        for outcome in OUTCOMES
    }


def _apply_calibration(
    probabilities: dict[str, float], calibration: tuple[float, float, float]
) -> dict[str, float]:
    slope, draw_bias, away_bias = calibration
    bias = {"HOME_WIN": 0.0, "DRAW": draw_bias, "AWAY_WIN": away_bias}
    logits = {
        outcome: slope * math.log(max(probabilities[outcome], PROBABILITY_FLOOR)) + bias[outcome]
        for outcome in OUTCOMES
    }
    top = max(logits.values())
    weights = {outcome: math.exp(logits[outcome] - top) for outcome in OUTCOMES}
    total = sum(weights.values())
    return {outcome: weights[outcome] / total for outcome in OUTCOMES}


def _solve3(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting for the 3x3 Newton step."""
    a = [row[:] + [value] for row, value in zip(matrix, vector)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ArithmeticError("singular recalibration system")
        a[col], a[pivot] = a[pivot], a[col]
        for row in range(col + 1, 3):
            factor = a[row][col] / a[col][col]
            for k in range(col, 4):
                a[row][k] -= factor * a[col][k]
    solution = [0.0, 0.0, 0.0]
    for row in (2, 1, 0):
        solution[row] = (a[row][3] - sum(a[row][k] * solution[k] for k in range(row + 1, 3))) / a[row][row]
    return solution


def _objective(
    samples: Sequence[tuple[dict[str, float], str]], theta: tuple[float, float, float]
) -> float:
    loss = sum(-math.log(_apply_calibration(p, theta)[y]) for p, y in samples)
    return loss + 0.5 * RECAL_L2 * sum((t - i) ** 2 for t, i in zip(theta, IDENTITY_CALIBRATION))


def fit_calibration(
    samples: Sequence[tuple[dict[str, float], str]],
) -> tuple[float, float, float]:
    """Ridge-penalized multinomial recalibration on log-probabilities.

    ``q_k ∝ p_k ** slope * exp(bias_k)`` with the HOME_WIN bias fixed at 0, fitted by damped
    Newton steps on summed log loss plus ``RECAL_L2 / 2 * ||theta - identity||^2``. Returns the
    identity when there are fewer than ``RECAL_MIN_RESULTS`` samples or the fit fails.
    """
    if len(samples) < RECAL_MIN_RESULTS:
        return IDENTITY_CALIBRATION
    theta = IDENTITY_CALIBRATION
    current = _objective(samples, theta)
    for _ in range(RECAL_MAX_ITERATIONS):
        gradient = [RECAL_L2 * (t - i) for t, i in zip(theta, IDENTITY_CALIBRATION)]
        hessian = [[RECAL_L2 if r == c else 0.0 for c in range(3)] for r in range(3)]
        for probabilities, actual in samples:
            q = _apply_calibration(probabilities, theta)
            logs = [math.log(max(probabilities[o], PROBABILITY_FLOOR)) for o in OUTCOMES]
            # d z_k / d theta for theta = (slope, draw bias, away bias)
            jac = [[logs[k], float(k == 1), float(k == 2)] for k in range(3)]
            qs = [q[o] for o in OUTCOMES]
            for k in range(3):
                residual = qs[k] - float(OUTCOMES[k] == actual)
                for j in range(3):
                    gradient[j] += residual * jac[k][j]
            # J^T (diag(q) - q q^T) J
            mean = [sum(qs[k] * jac[k][j] for k in range(3)) for j in range(3)]
            for r in range(3):
                for c in range(3):
                    hessian[r][c] += sum(qs[k] * jac[k][r] * jac[k][c] for k in range(3)) - mean[r] * mean[c]
        try:
            step = _solve3(hessian, gradient)
        except ArithmeticError:
            return IDENTITY_CALIBRATION
        scale = 1.0
        improved = False
        for _halving in range(20):
            candidate = tuple(t - scale * s for t, s in zip(theta, step))
            value = _objective(samples, candidate)  # type: ignore[arg-type]
            if value <= current:
                improved = True
                break
            scale /= 2.0
        if not improved:
            break
        change = max(abs(c - t) for c, t in zip(candidate, theta))
        theta, current = candidate, value  # type: ignore[assignment]
        if change < 1e-9:
            break
    slope = min(max(theta[0], RECAL_SLOPE_BOUNDS[0]), RECAL_SLOPE_BOUNDS[1])
    draw_bias = min(max(theta[1], -RECAL_BIAS_BOUND), RECAL_BIAS_BOUND)
    away_bias = min(max(theta[2], -RECAL_BIAS_BOUND), RECAL_BIAS_BOUND)
    if not all(math.isfinite(value) for value in (slope, draw_bias, away_bias)):
        return IDENTITY_CALIBRATION
    return (slope, draw_bias, away_bias)


def _history_digest(results: Sequence[RatingResult]) -> str:
    payload = {
        "constants": FROZEN_CONSTANTS,
        "results": [
            [
                item.fixture_id,
                normalize_team_name(item.home_team),
                normalize_team_name(item.away_team),
                _stamp(item.kickoff_at),
                _stamp(item.available_at),
                item.actual_outcome,
            ]
            for item in results
        ],
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_rating_state(results: Iterable[RatingResult], as_of: datetime) -> RatingState:
    """Sweep visible results in kickoff order; fit recalibration on prequential probabilities.

    Each result's prequential probability is computed from the ratings and base rate just
    before that result's own update, so the recalibration layer is fitted only on
    out-of-sample rating probabilities, and only on results visible at ``as_of``.
    """
    usable = visible_results(results, as_of)
    if not usable:
        raise ValueError("no results were available before prediction time")
    ratings: dict[str, float] = defaultdict(lambda: INITIAL_RATING)
    games: Counter[str] = Counter()
    counts: Counter[str] = Counter()
    samples: list[tuple[dict[str, float], str]] = []
    for item in usable:
        home, away = normalize_team_name(item.home_team), normalize_team_name(item.away_team)
        if home == away:
            raise ValueError("home and away team labels collide after normalization")
        if counts:
            samples.append((_shrunk(ratings, games, home, away, _smoothed(counts)), item.actual_outcome))
        ratings[home], ratings[away] = _elo_update(ratings[home], ratings[away], item.actual_outcome)
        games[home] += 1
        games[away] += 1
        counts[item.actual_outcome] += 1
    base_rate = _smoothed(counts)
    return RatingState(
        as_of=_utc(as_of, "as_of"),
        ratings=dict(ratings),
        games=dict(games),
        base_rate=base_rate,
        draw_rate=base_rate["DRAW"],
        calibration=fit_calibration(samples),
        results_used=len(usable),
        history_sha256=_history_digest(usable),
    )


def rating_probabilities(state: RatingState, home_team: str, away_team: str) -> dict[str, float]:
    """Final, recalibrated three-way probabilities for one fixture."""
    home, away = normalize_team_name(home_team), normalize_team_name(away_team)
    if home == away:
        raise ValueError("home and away team labels collide after normalization")
    raw = _shrunk(state.ratings, state.games, home, away, state.base_rate)
    calibrated = _apply_calibration(raw, state.calibration)
    floored = {outcome: max(calibrated[outcome], PROBABILITY_FLOOR) for outcome in OUTCOMES}
    total = math.fsum(floored.values())
    probabilities = {outcome: floored[outcome] / total for outcome in OUTCOMES}
    if not all(math.isfinite(value) for value in probabilities.values()):
        raise ValueError("rating probabilities are not finite")
    return probabilities


def create_rating_prediction(
    fixture,
    state: RatingState,
    *,
    prediction_id: UUID,
    predicted_at: datetime,
) -> PredictionRecord:
    """Create an immutable prediction under the rating model's own identity.

    ``fixture`` is a ``proracle.domain.NormalizedFixture``. ``state`` must have been built for
    exactly ``predicted_at`` so it cannot contain any later result.
    """
    prediction_time = _utc(predicted_at, "predicted_at")
    if state.as_of != prediction_time:
        raise ValueError("rating state must be built at the prediction time")
    if prediction_time >= fixture.kickoff_at:
        raise ValueError("prediction must be created before kickoff")
    probabilities = rating_probabilities(state, fixture.home_team, fixture.away_team)
    snapshot = json.dumps(
        {
            "history_sha256": state.history_sha256,
            "home": normalize_team_name(fixture.home_team),
            "away": normalize_team_name(fixture.away_team),
            "as_of": _stamp(state.as_of),
            "calibration": list(state.calibration),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    record = {
        "contract_version": "1.0",
        "prediction_id": str(prediction_id),
        "fixture_id": str(fixture.fixture_id),
        "predicted_at": _stamp(prediction_time),
        "kickoff_at": _stamp(fixture.kickoff_at),
        "model": {
            "provider": RATING_MODEL_PROVIDER,
            "name": RATING_MODEL_NAME,
            "version": RATING_MODEL_VERSION,
        },
        "prompt_version": RATING_PROMPT_VERSION,
        "input_schema_version": RATING_INPUT_SCHEMA_VERSION,
        "input_snapshot_hash": hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
        "probabilities": probabilities,
    }
    return parse_prediction_record(record)
