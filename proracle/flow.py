"""Dependency-injected offline flow; production providers/stores can be adapters."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from proracle.domain import (
    EvaluationRow,
    NormalizedFixture,
    attach_final_result,
    create_baseline_prediction,
    normalize_fixture,
    validate_features,
)
from proracle.evaluate import Result
from proracle.contracts import PredictionRecord


class FixtureProvider(Protocol):
    def upcoming_fixtures(self) -> Iterable[Mapping[str, object]]: ...


class FeatureProvider(Protocol):
    def features_for(self, fixture_id: UUID) -> Mapping[str, object]: ...


class FinalResultProvider(Protocol):
    def final_result_for(self, fixture_id: UUID) -> Mapping[str, object] | None: ...


class PredictionStore(Protocol):
    def append_prediction(self, record: PredictionRecord) -> None: ...


class PredictionClock(Protocol):
    def now_for(self, fixture: NormalizedFixture) -> datetime: ...


class PredictionIdFactory(Protocol):
    def for_fixture(self, fixture: NormalizedFixture) -> UUID: ...


def run_offline_flow(
    fixtures: FixtureProvider,
    features: FeatureProvider,
    results: FinalResultProvider,
    prediction_store: PredictionStore,
    training_results: Sequence[Result],
    clock: PredictionClock,
    id_factory: PredictionIdFactory,
) -> tuple[list[PredictionRecord], list[EvaluationRow]]:
    """Normalize inputs, append predictions, and export rows for final results.

    Adapters are injected at the boundary. Pull-request tests use in-memory fakes;
    this function itself performs no network, database, model, or file I/O.
    """
    predictions: list[PredictionRecord] = []
    evaluation_rows: list[EvaluationRow] = []
    seen_fixture_ids: set[UUID] = set()
    for raw_fixture in fixtures.upcoming_fixtures():
        fixture = normalize_fixture(raw_fixture)
        if fixture.fixture_id in seen_fixture_ids:
            raise ValueError(f"duplicate fixture_id: {fixture.fixture_id}")
        seen_fixture_ids.add(fixture.fixture_id)
        feature_snapshot = validate_features(features.features_for(fixture.fixture_id))
        prediction = create_baseline_prediction(
            fixture,
            feature_snapshot,
            training_results,
            prediction_id=id_factory.for_fixture(fixture),
            predicted_at=clock.now_for(fixture),
        )
        prediction_store.append_prediction(prediction)
        predictions.append(prediction)

        result = results.final_result_for(fixture.fixture_id)
        if result is not None and result.get("status") == "FINAL":
            evaluation_rows.append(attach_final_result(prediction, result))
    return predictions, evaluation_rows
