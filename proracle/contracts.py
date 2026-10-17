"""Validate immutable PRORACLE prediction and prediction-failure records."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

CONTRACT_VERSION = "1.0"
INPUT_SCHEMA_VERSIONS = frozenset({"features-v1"})
OUTCOMES = ("HOME_WIN", "DRAW", "AWAY_WIN")
PROBABILITY_SUM_TOLERANCE = 1e-6

PREDICTION_FIELDS = frozenset(
    {
        "contract_version",
        "prediction_id",
        "fixture_id",
        "predicted_at",
        "kickoff_at",
        "model",
        "prompt_version",
        "input_schema_version",
        "input_snapshot_hash",
        "probabilities",
    }
)
MODEL_FIELDS = frozenset({"provider", "name", "version"})
PROBABILITY_FIELDS = frozenset(OUTCOMES)
FAILURE_FIELDS = frozenset(
    {
        "contract_version",
        "event_type",
        "event_id",
        "fixture_id",
        "occurred_at",
        "kickoff_at",
        "stage",
        "error_code",
        "retryable",
    }
)
FAILURE_STAGES = frozenset(
    {
        "INPUT_VALIDATION",
        "SOURCE_REQUEST",
        "MODEL_REQUEST",
        "RESPONSE_VALIDATION",
        "PERSISTENCE",
    }
)
FAILURE_CODES_BY_STAGE = {
    "INPUT_VALIDATION": frozenset({"INVALID_INPUT", "FEATURES_MISSING", "FEATURES_STALE"}),
    "SOURCE_REQUEST": frozenset(
        {
            "SOURCE_TIMEOUT",
            "SOURCE_RATE_LIMITED",
            "SOURCE_QUOTA_EXHAUSTED",
            "SOURCE_UNAVAILABLE",
            "SOURCE_UNAUTHORIZED",
            "SOURCE_INVALID_RESPONSE",
        }
    ),
    "MODEL_REQUEST": frozenset(
        {"MODEL_TIMEOUT", "MODEL_RATE_LIMIT", "MODEL_UNAVAILABLE", "COST_LIMIT_REACHED"}
    ),
    "RESPONSE_VALIDATION": frozenset({"MODEL_INVALID_OUTPUT"}),
    "PERSISTENCE": frozenset({"PERSISTENCE_FAILED"}),
}
_UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


@dataclass(frozen=True)
class ProbabilityVector:
    """Finite three-way probabilities in the contract's fixed outcome order."""

    home_win: float
    draw: float
    away_win: float

    def as_dict(self) -> dict[str, float]:
        return {
            "HOME_WIN": self.home_win,
            "DRAW": self.draw,
            "AWAY_WIN": self.away_win,
        }

    @property
    def predicted_outcome(self) -> str:
        """Return the maximum-probability outcome with deterministic tie order."""
        return max(OUTCOMES, key=self.as_dict().__getitem__)


@dataclass(frozen=True)
class ModelDescriptor:
    provider: str
    name: str
    version: str

    def as_dict(self) -> dict[str, str]:
        return {"provider": self.provider, "name": self.name, "version": self.version}


@dataclass(frozen=True)
class PredictionRecord:
    contract_version: str
    prediction_id: UUID
    fixture_id: UUID
    predicted_at: datetime
    kickoff_at: datetime
    model: ModelDescriptor
    prompt_version: str
    input_schema_version: str
    input_snapshot_hash: str
    probabilities: ProbabilityVector

    def as_dict(self) -> dict[str, Any]:
        """Serialize to the stable JSON-compatible record shape."""
        return {
            "contract_version": self.contract_version,
            "prediction_id": str(self.prediction_id),
            "fixture_id": str(self.fixture_id),
            "predicted_at": _format_timestamp(self.predicted_at),
            "kickoff_at": _format_timestamp(self.kickoff_at),
            "model": self.model.as_dict(),
            "prompt_version": self.prompt_version,
            "input_schema_version": self.input_schema_version,
            "input_snapshot_hash": self.input_snapshot_hash,
            "probabilities": self.probabilities.as_dict(),
        }


@dataclass(frozen=True)
class PredictionFailureEvent:
    """Safe, structured failure metadata; never contains provider text or payloads."""

    contract_version: str
    event_id: UUID
    fixture_id: UUID
    occurred_at: datetime
    kickoff_at: datetime
    stage: str
    error_code: str
    retryable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "event_type": "prediction_failed",
            "event_id": str(self.event_id),
            "fixture_id": str(self.fixture_id),
            "occurred_at": _format_timestamp(self.occurred_at),
            "kickoff_at": _format_timestamp(self.kickoff_at),
            "stage": self.stage,
            "error_code": self.error_code,
            "retryable": self.retryable,
        }


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if any(not isinstance(field, str) for field in value):
        raise ValueError(f"{label} field names must be strings")
    actual = set(value)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing fields: {', '.join(sorted(missing))}")
        if extra:
            details.append(f"unexpected fields: {', '.join(sorted(extra))}")
        raise ValueError(f"{label} has {'; '.join(details)}")


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label} must be a non-empty string without surrounding whitespace")
    return value


def _uuid(value: Any, label: str) -> UUID:
    text = _text(value, label)
    if not _UUID_PATTERN.fullmatch(text):
        raise ValueError(f"{label} must be a lowercase, hyphenated UUID")
    try:
        parsed = UUID(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid UUID") from exc
    if str(parsed) != text:
        raise ValueError(f"{label} must be a canonical UUID")
    return parsed


def _timestamp(value: Any, label: str) -> datetime:
    text = _text(value, label)
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="auto").replace(
        "+00:00", "Z"
    )


def _probabilities(value: Any) -> ProbabilityVector:
    probabilities = _object(value, "probabilities")
    _exact_fields(probabilities, PROBABILITY_FIELDS, "probabilities")
    parsed: dict[str, float] = {}
    for outcome in OUTCOMES:
        raw_probability = probabilities[outcome]
        if isinstance(raw_probability, bool) or not isinstance(raw_probability, (int, float)):
            raise ValueError(f"probabilities.{outcome} must be a number")
        try:
            probability = float(raw_probability)
        except OverflowError as exc:
            raise ValueError(f"probabilities.{outcome} is outside the numeric range") from exc
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError(f"probabilities.{outcome} must be finite and between 0 and 1")
        parsed[outcome] = probability
    if not math.isclose(
        math.fsum(parsed.values()),
        1.0,
        rel_tol=0.0,
        abs_tol=PROBABILITY_SUM_TOLERANCE,
    ):
        raise ValueError("probabilities must sum to 1 within an absolute tolerance of 1e-6")
    return ProbabilityVector(
        home_win=parsed["HOME_WIN"],
        draw=parsed["DRAW"],
        away_win=parsed["AWAY_WIN"],
    )


def parse_prediction_record(value: Any) -> PredictionRecord:
    """Validate a successful immutable prediction record from a decoded object."""
    record = _object(value, "prediction")
    _exact_fields(record, PREDICTION_FIELDS, "prediction")
    contract_version = _text(record["contract_version"], "contract_version")
    if contract_version != CONTRACT_VERSION:
        raise ValueError(f"contract_version must be {CONTRACT_VERSION!r}")

    input_schema_version = _text(record["input_schema_version"], "input_schema_version")
    if input_schema_version not in INPUT_SCHEMA_VERSIONS:
        raise ValueError("input_schema_version is not recognized")

    model_data = _object(record["model"], "model")
    _exact_fields(model_data, MODEL_FIELDS, "model")
    model = ModelDescriptor(
        provider=_text(model_data["provider"], "model.provider"),
        name=_text(model_data["name"], "model.name"),
        version=_text(model_data["version"], "model.version"),
    )

    predicted_at = _timestamp(record["predicted_at"], "predicted_at")
    kickoff_at = _timestamp(record["kickoff_at"], "kickoff_at")
    if predicted_at >= kickoff_at:
        raise ValueError("predicted_at must be strictly before kickoff_at")

    input_snapshot_hash = _text(record["input_snapshot_hash"], "input_snapshot_hash")
    if not _HASH_PATTERN.fullmatch(input_snapshot_hash):
        raise ValueError("input_snapshot_hash must be 64 lowercase hexadecimal characters")

    return PredictionRecord(
        contract_version=contract_version,
        prediction_id=_uuid(record["prediction_id"], "prediction_id"),
        fixture_id=_uuid(record["fixture_id"], "fixture_id"),
        predicted_at=predicted_at,
        kickoff_at=kickoff_at,
        model=model,
        prompt_version=_text(record["prompt_version"], "prompt_version"),
        input_schema_version=input_schema_version,
        input_snapshot_hash=input_snapshot_hash,
        probabilities=_probabilities(record["probabilities"]),
    )


def parse_prediction_failure_event(value: Any) -> PredictionFailureEvent:
    """Validate a failure event without accepting raw exception/provider data."""
    event = _object(value, "failure event")
    _exact_fields(event, FAILURE_FIELDS, "failure event")
    contract_version = _text(event["contract_version"], "contract_version")
    if contract_version != CONTRACT_VERSION:
        raise ValueError(f"contract_version must be {CONTRACT_VERSION!r}")
    if event["event_type"] != "prediction_failed":
        raise ValueError("event_type must be 'prediction_failed'")

    stage = _text(event["stage"], "stage")
    if stage not in FAILURE_STAGES:
        raise ValueError("stage is not recognized")
    error_code = _text(event["error_code"], "error_code")
    if not _ERROR_CODE_PATTERN.fullmatch(error_code):
        raise ValueError("error_code must be a stable uppercase code, not free-form text")
    if error_code not in FAILURE_CODES_BY_STAGE[stage]:
        raise ValueError("error_code is not allowed for this failure stage")
    retryable = event["retryable"]
    if not isinstance(retryable, bool):
        raise ValueError("retryable must be a boolean")

    occurred_at = _timestamp(event["occurred_at"], "occurred_at")
    kickoff_at = _timestamp(event["kickoff_at"], "kickoff_at")
    if occurred_at >= kickoff_at:
        raise ValueError("occurred_at must be strictly before kickoff_at")

    return PredictionFailureEvent(
        contract_version=contract_version,
        event_id=_uuid(event["event_id"], "event_id"),
        fixture_id=_uuid(event["fixture_id"], "fixture_id"),
        occurred_at=occurred_at,
        kickoff_at=kickoff_at,
        stage=stage,
        error_code=error_code,
        retryable=retryable,
    )
