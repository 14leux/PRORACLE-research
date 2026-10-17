"""Run the fully synthetic offline fixture-to-evaluation demonstration."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from proracle.evaluate import Prediction, evaluate, load_training_results
from proracle.flow import run_offline_flow

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "data" / "synthetic"


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: each row must be an object")
        rows.append(row)
    return rows


class _FixtureProvider:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def upcoming_fixtures(self):
        return iter(self.rows)


class _FeatureProvider:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {row["fixture_id"]: row["features"] for row in rows}

    def features_for(self, fixture_id: UUID):
        try:
            return self.rows[str(fixture_id)]
        except KeyError as exc:
            raise ValueError(f"missing synthetic feature row for {fixture_id}") from exc


class _FinalResultProvider:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {row["fixture_id"]: row for row in rows}

    def final_result_for(self, fixture_id: UUID):
        return self.rows.get(str(fixture_id))


class _PredictionStore:
    def __init__(self) -> None:
        self.rows = []

    def append_prediction(self, record):
        if any(existing.prediction_id == record.prediction_id for existing in self.rows):
            raise ValueError(f"duplicate prediction id: {record.prediction_id}")
        self.rows.append(record)


class _Clock:
    def now_for(self, fixture):
        return fixture.kickoff_at - timedelta(hours=3)


class _PredictionIds:
    def for_fixture(self, fixture):
        return uuid5(NAMESPACE_URL, f"proracle-synthetic-demo:{fixture.fixture_id}")


def run_demo() -> dict[str, Any]:
    """Execute the injectable flow against fabricated JSONL and in-memory fakes."""
    training = load_training_results(SYNTHETIC / "training_results.jsonl")
    store = _PredictionStore()
    predictions, rows = run_offline_flow(
        _FixtureProvider(_load_jsonl(SYNTHETIC / "fixtures.jsonl")),
        _FeatureProvider(_load_jsonl(SYNTHETIC / "features.jsonl")),
        _FinalResultProvider(_load_jsonl(SYNTHETIC / "final_results.jsonl")),
        store,
        training,
        _Clock(),
        _PredictionIds(),
    )
    evaluation_predictions = [
        Prediction(
            fixture_id=str(row.fixture_id),
            kickoff_at=row.kickoff_at,
            actual_outcome=row.actual_outcome,
            prediction_at=row.prediction_at,
            probabilities=row.as_dict()["probabilities"],
        )
        for row in rows
    ]
    return {
        "data_kind": "synthetic",
        "note": "Fabricated data validates the flow only; these scores are not sports-performance evidence.",
        "predictions_created": len(predictions),
        "final_results_attached": len(rows),
        "evaluation": evaluate(training, evaluation_predictions),
        "predictions": [prediction.as_dict() for prediction in predictions],
    }


def main() -> int:
    try:
        print(json.dumps(run_demo(), indent=2, sort_keys=True, allow_nan=False))
    except (OSError, ValueError) as exc:
        print(f"synthetic demo error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
