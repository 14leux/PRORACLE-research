"""Deterministic, provider-independent prediction and result domain functions."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from numbers import Real
from typing import Any
from uuid import UUID

from proracle.baselines import add_one_smoothed_probabilities
from proracle.contracts import OUTCOMES, PredictionRecord, parse_prediction_record
from proracle.evaluate import Result

_FIXTURE_FIELDS = frozenset(
    {"fixture_id", "home_team", "away_team", "kickoff_at", "competition_id", "competition_name"}
)
_FEATURE_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RESULT_FIELDS = frozenset(
    {
        "id",
        "fixture_id",
        "supersedes_result_id",
        "status",
        "actual_outcome",
        "observed_at",
        "finalized_at",
        "source_name",
        "source_retrieved_at",
    }
)


@dataclass(frozen=True)
class NormalizedFixture:
    fixture_id: UUID
    home_team: str
    away_team: str
    kickoff_at: datetime
    competition_id: str | None = None
    competition_name: str | None = None


@dataclass(frozen=True)
class FeatureSnapshot:
    """A validated synthetic feature vector and its canonical local digest."""

    entries: tuple[tuple[str, float], ...]
    sha256: str

    def as_dict(self) -> dict[str, float]:
        return dict(self.entries)


@dataclass(frozen=True)
class EvaluationRow:
    fixture_id: UUID
    kickoff_at: datetime
    prediction_at: datetime
    actual_outcome: str
    probability_values: tuple[float, float, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": str(self.fixture_id),
            "kickoff_at": _format_timestamp(self.kickoff_at),
            "prediction_at": _format_timestamp(self.prediction_at),
            "actual_outcome": self.actual_outcome,
            "probabilities": dict(zip(OUTCOMES, self.probability_values, strict=True)),
        }


def _timestamp(value: object, label: str) -> datetime:
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


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _team_name(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def normalize_fixture(value: Mapping[str, object]) -> NormalizedFixture:
    """Copy only the supported fixture fields and normalize names/times locally."""
    if not isinstance(value, Mapping):
        raise ValueError("fixture must be an object")
    missing = {"fixture_id", "home_team", "away_team", "kickoff_at"} - set(value)
    extra = set(value) - _FIXTURE_FIELDS
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(sorted(missing))}")
        if extra:
            details.append(f"unsupported fields: {', '.join(sorted(extra))}")
        raise ValueError(f"fixture has {'; '.join(details)}")
    raw_id = value["fixture_id"]
    if not isinstance(raw_id, str):
        raise ValueError("fixture_id must be a canonical UUID string")
    try:
        fixture_id = UUID(raw_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("fixture_id must be a canonical UUID string") from exc
    if str(fixture_id) != raw_id:
        raise ValueError("fixture_id must be a canonical lowercase UUID string")
    home = _team_name(value["home_team"], "home_team")
    away = _team_name(value["away_team"], "away_team")
    if home.casefold() == away.casefold():
        raise ValueError("home_team and away_team must identify different teams")

    def optional_label(key: str) -> str | None:
        item = value.get(key)
        if item is None:
            return None
        label = _team_name(item, key)
        return label

    return NormalizedFixture(
        fixture_id=fixture_id,
        home_team=home,
        away_team=away,
        kickoff_at=_timestamp(value["kickoff_at"], "kickoff_at"),
        competition_id=optional_label("competition_id"),
        competition_name=optional_label("competition_name"),
    )


def validate_features(value: Mapping[str, object]) -> FeatureSnapshot:
    """Validate and hash generic numeric features without fixing provider fields."""
    if not isinstance(value, Mapping) or not value:
        raise ValueError("features must be a non-empty object")
    if len(value) > 64:
        raise ValueError("features may contain at most 64 values")
    normalized: dict[str, float] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not _FEATURE_KEY.fullmatch(key):
            raise ValueError("feature names must be lowercase snake_case identifiers")
        if isinstance(raw, bool) or not isinstance(raw, Real):
            raise ValueError(f"feature {key!r} must be a finite number")
        number = float(raw)
        if not math.isfinite(number):
            raise ValueError(f"feature {key!r} must be a finite number")
        normalized[key] = number
    ordered = tuple(sorted(normalized.items()))
    canonical = json.dumps(dict(ordered), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return FeatureSnapshot(entries=ordered, sha256=digest)


def create_baseline_prediction(
    fixture: NormalizedFixture,
    features: FeatureSnapshot,
    training_results: Sequence[Result],
    *,
    prediction_id: UUID,
    predicted_at: datetime,
) -> PredictionRecord:
    """Create an immutable prematch prediction from only earlier final outcomes."""
    if predicted_at.tzinfo is None or predicted_at.utcoffset() is None:
        raise ValueError("predicted_at must include a UTC offset")
    prediction_time = predicted_at.astimezone(timezone.utc)
    if prediction_time >= fixture.kickoff_at:
        raise ValueError("prediction must be created before kickoff")
    prior = []
    for result in training_results:
        available_at = result.available_at or result.kickoff_at
        if available_at.tzinfo is None or available_at.utcoffset() is None:
            raise ValueError("training result availability time must include a UTC offset")
        if available_at.astimezone(timezone.utc) < prediction_time:
            prior.append(result)
    if not prior:
        raise ValueError("no historical outcomes were available before prediction time")
    probabilities = add_one_smoothed_probabilities(
        result.actual_outcome for result in prior
    )
    record = {
        "contract_version": "1.0",
        "prediction_id": str(prediction_id),
        "fixture_id": str(fixture.fixture_id),
        "predicted_at": _format_timestamp(prediction_time),
        "kickoff_at": _format_timestamp(fixture.kickoff_at),
        "model": {"provider": "proracle", "name": "historical_frequency", "version": "1"},
        "prompt_version": "not_applicable",
        "input_schema_version": "features-v1",
        "input_snapshot_hash": features.sha256,
        "probabilities": probabilities,
    }
    return parse_prediction_record(record)


def attach_final_result(
    prediction: PredictionRecord, result: Mapping[str, object]
) -> EvaluationRow:
    """Join one final result to a matching immutable prediction for evaluation."""
    if not isinstance(result, Mapping):
        raise ValueError("result must be an object")
    missing = {"fixture_id", "status", "actual_outcome", "finalized_at"} - set(result)
    extra = set(result) - _RESULT_FIELDS
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(sorted(missing))}")
        if extra:
            details.append(f"unsupported fields: {', '.join(sorted(extra))}")
        raise ValueError(f"result has {'; '.join(details)}")
    if result["fixture_id"] != str(prediction.fixture_id):
        raise ValueError("result fixture_id does not match prediction fixture_id")
    if result["status"] != "FINAL":
        raise ValueError("only FINAL results can be attached for scoring")
    actual = result["actual_outcome"]
    if actual not in OUTCOMES:
        raise ValueError("FINAL result actual_outcome must be a supported 1X2 outcome")
    finalized_at = _timestamp(result["finalized_at"], "finalized_at")
    if finalized_at < prediction.kickoff_at:
        raise ValueError("finalized_at must be at or after kickoff")
    probabilities = prediction.probabilities
    return EvaluationRow(
        fixture_id=prediction.fixture_id,
        kickoff_at=prediction.kickoff_at,
        prediction_at=prediction.predicted_at,
        actual_outcome=actual,
        probability_values=(probabilities.home_win, probabilities.draw, probabilities.away_win),
    )
