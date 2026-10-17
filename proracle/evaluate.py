"""Evaluate three-way sports predictions without network or database access."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from proracle.baselines import add_one_smoothed_probabilities

OUTCOMES = ("HOME_WIN", "DRAW", "AWAY_WIN")
LOG_FLOOR = 1e-15
PROBABILITY_TOLERANCE = 1e-6
CALIBRATION_BIN_COUNT = 10


@dataclass(frozen=True)
class Result:
    fixture_id: str
    kickoff_at: datetime
    actual_outcome: str
    available_at: datetime | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class Prediction(Result):
    prediction_at: datetime
    probabilities: dict[str, float]


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 timestamp string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _outcome(value: Any, label: str) -> str:
    if value not in OUTCOMES:
        raise ValueError(f"{label} must be one of {', '.join(OUTCOMES)}")
    return value


def _fixture_id(row: dict[str, Any], label: str) -> str:
    value = row.get("fixture_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.fixture_id must be a non-empty string")
    return value.strip()


def load_training_results(path: Path) -> list[Result]:
    """Load final outcomes used only to estimate the historical-frequency baseline."""
    records: list[Result] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        label = f"{path}:{line_number}"
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label}: invalid JSON ({exc.msg})") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label}: each line must be a JSON object")
        fixture_id = _fixture_id(row, label)
        if fixture_id in seen:
            raise ValueError(f"{label}: duplicate fixture_id {fixture_id!r}")
        seen.add(fixture_id)
        records.append(
            Result(
                fixture_id=fixture_id,
                kickoff_at=_timestamp(row.get("kickoff_at"), f"{label}.kickoff_at"),
                actual_outcome=_outcome(row.get("actual_outcome"), f"{label}.actual_outcome"),
                available_at=(
                    _timestamp(row["available_at"], f"{label}.available_at")
                    if row.get("available_at") is not None
                    else None
                ),
            )
        )
        if records[-1].available_at is not None and records[-1].available_at < records[-1].kickoff_at:
            raise ValueError(f"{label}.available_at must not precede kickoff_at")
    return records


def _probabilities(value: Any, label: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(OUTCOMES):
        raise ValueError(f"{label} must contain exactly {', '.join(OUTCOMES)}")
    result: dict[str, float] = {}
    for outcome in OUTCOMES:
        probability = value[outcome]
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            raise ValueError(f"{label}.{outcome} must be a number")
        try:
            probability = float(probability)
        except OverflowError as exc:
            raise ValueError(f"{label}.{outcome} is outside the supported numeric range") from exc
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError(f"{label}.{outcome} must be finite and between 0 and 1")
        result[outcome] = probability
    if not math.isclose(sum(result.values()), 1.0, abs_tol=PROBABILITY_TOLERANCE):
        raise ValueError(f"{label} probabilities must sum to 1")
    return result


def load_predictions(path: Path) -> list[Prediction]:
    """Load evaluated predictions and verify each was recorded before kickoff."""
    records: list[Prediction] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        label = f"{path}:{line_number}"
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label}: invalid JSON ({exc.msg})") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label}: each line must be a JSON object")
        fixture_id = _fixture_id(row, label)
        if fixture_id in seen:
            raise ValueError(f"{label}: duplicate fixture_id {fixture_id!r}")
        seen.add(fixture_id)
        kickoff_at = _timestamp(row.get("kickoff_at"), f"{label}.kickoff_at")
        prediction_at = _timestamp(row.get("prediction_at"), f"{label}.prediction_at")
        if prediction_at >= kickoff_at:
            raise ValueError(f"{label}: prediction_at must be before kickoff_at")
        records.append(
            Prediction(
                fixture_id=fixture_id,
                kickoff_at=kickoff_at,
                actual_outcome=_outcome(row.get("actual_outcome"), f"{label}.actual_outcome"),
                prediction_at=prediction_at,
                probabilities=_probabilities(row.get("probabilities"), f"{label}.probabilities"),
            )
        )
    return records


def _scores(records: list[Prediction], baseline: dict[str, float] | None = None) -> dict[str, Any]:
    confusion = {actual: {predicted: 0 for predicted in OUTCOMES} for actual in OUTCOMES}
    calibration_bins = {
        outcome: [[] for _ in range(CALIBRATION_BIN_COUNT)] for outcome in OUTCOMES
    }
    correct = 0
    log_loss = 0.0
    brier = 0.0
    for record in records:
        probabilities = baseline if baseline is not None else record.probabilities
        predicted = max(OUTCOMES, key=probabilities.__getitem__)
        confusion[record.actual_outcome][predicted] += 1
        correct += predicted == record.actual_outcome
        actual_probability = probabilities[record.actual_outcome]
        log_loss -= math.log(max(actual_probability, LOG_FLOOR))
        brier += sum(
            (probabilities[outcome] - (outcome == record.actual_outcome)) ** 2
            for outcome in OUTCOMES
        )
        for outcome in OUTCOMES:
            probability = probabilities[outcome]
            bin_index = min(int(probability * CALIBRATION_BIN_COUNT), CALIBRATION_BIN_COUNT - 1)
            calibration_bins[outcome][bin_index].append(
                (probability, int(outcome == record.actual_outcome))
            )
    count = len(records)
    reliability = {}
    calibration_error = 0.0
    for outcome in OUTCOMES:
        outcome_bins = []
        for index, entries in enumerate(calibration_bins[outcome]):
            if not entries:
                continue
            mean_probability = sum(probability for probability, _ in entries) / len(entries)
            observed_rate = sum(observed for _, observed in entries) / len(entries)
            calibration_error += (len(entries) / (count * len(OUTCOMES))) * abs(
                mean_probability - observed_rate
            )
            outcome_bins.append(
                {
                    "lower_bound": index / CALIBRATION_BIN_COUNT,
                    "upper_bound": (index + 1) / CALIBRATION_BIN_COUNT,
                    "fixtures": len(entries),
                    "mean_probability": mean_probability,
                    "observed_rate": observed_rate,
                }
            )
        reliability[outcome] = outcome_bins
    return {
        "fixtures": count,
        "accuracy": correct / count,
        "log_loss": log_loss / count,
        "brier_score": brier / count,
        "classwise_expected_calibration_error": calibration_error,
        "reliability_bins": reliability,
        "confusion_matrix": confusion,
    }


def evaluate(training: list[Result], predictions: list[Prediction]) -> dict[str, Any]:
    """Compare candidate predictions with an add-one-smoothed class-prior baseline."""
    if not training:
        raise ValueError("at least one training result is required")
    if not predictions:
        raise ValueError("at least one prediction is required")
    latest_training_result = max(result.available_at or result.kickoff_at for result in training)
    earliest_prediction_time = min(prediction.prediction_at for prediction in predictions)
    if latest_training_result >= earliest_prediction_time:
        raise ValueError("all training results must be final before every evaluated prediction")

    counts = Counter(result.actual_outcome for result in training)
    baseline = add_one_smoothed_probabilities(result.actual_outcome for result in training)
    return {
        "training_fixtures": len(training),
        "evaluation_fixtures": len(predictions),
        "training_outcome_counts": {outcome: counts[outcome] for outcome in OUTCOMES},
        "historical_frequency_baseline_probabilities": baseline,
        "model": _scores(predictions),
        "historical_frequency_baseline": _scores(predictions, baseline),
        "metric_notes": {
            "log_loss": "Mean negative natural log probability assigned to the actual outcome; zero probabilities are floored at 1e-15.",
            "brier_score": "Mean sum of squared errors across the three outcome probabilities; lower is better.",
            "classwise_expected_calibration_error": "Ten-bin classwise expected calibration error, averaged across outcomes; small cohorts make this descriptive only.",
            "baseline": "Add-one-smoothed outcome frequencies from training results, held fixed for the evaluation cohort.",
            "ties": "Predicted class ties resolve in this order: HOME_WIN, DRAW, AWAY_WIN.",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-results", type=Path, required=True, help="JSONL final outcomes before the evaluation cohort")
    parser.add_argument("--predictions", type=Path, required=True, help="JSONL predictions for the later evaluation cohort")
    args = parser.parse_args(argv)
    try:
        report = evaluate(load_training_results(args.training_results), load_predictions(args.predictions))
    except (OSError, ValueError) as exc:
        print(f"evaluation error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
