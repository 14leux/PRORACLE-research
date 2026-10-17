"""Offline validation tests for the agreed prediction contract."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from proracle.contracts import (
    PROBABILITY_SUM_TOLERANCE,
    parse_prediction_failure_event,
    parse_prediction_record,
)


def prediction_record() -> dict[str, object]:
    return {
        "contract_version": "1.0",
        "prediction_id": "00000000-0000-4000-8000-000000000001",
        "fixture_id": "00000000-0000-4000-8000-000000000002",
        "predicted_at": "2026-10-06T12:00:00Z",
        "kickoff_at": "2026-10-06T18:00:00+00:00",
        "model": {"provider": "example", "name": "baseline", "version": "1"},
        "prompt_version": "baseline-v1",
        "input_schema_version": "features-v1",
        "input_snapshot_hash": "a" * 64,
        "probabilities": {"HOME_WIN": 0.48, "DRAW": 0.27, "AWAY_WIN": 0.25},
    }


def failure_event() -> dict[str, object]:
    return {
        "contract_version": "1.0",
        "event_type": "prediction_failed",
        "event_id": "00000000-0000-4000-8000-000000000003",
        "fixture_id": "00000000-0000-4000-8000-000000000002",
        "occurred_at": "2026-10-06T12:01:00Z",
        "kickoff_at": "2026-10-06T18:00:00Z",
        "stage": "MODEL_REQUEST",
        "error_code": "MODEL_TIMEOUT",
        "retryable": True,
    }


class PredictionRecordTests(unittest.TestCase):
    def test_valid_record_normalizes_times_and_derives_tied_outcome(self) -> None:
        record = parse_prediction_record(prediction_record())
        self.assertEqual(record.predicted_at.isoformat(), "2026-10-06T12:00:00+00:00")
        self.assertEqual(record.as_dict()["predicted_at"], "2026-10-06T12:00:00Z")
        self.assertEqual(record.probabilities.predicted_outcome, "HOME_WIN")
        self.assertEqual(record.as_dict()["probabilities"]["DRAW"], 0.27)

    def test_record_and_nested_values_are_immutable(self) -> None:
        record = parse_prediction_record(prediction_record())
        with self.assertRaises(FrozenInstanceError):
            record.prompt_version = "changed"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            record.probabilities.draw = 0.1  # type: ignore[misc]
        serialized = record.as_dict()
        serialized["probabilities"]["DRAW"] = 0.1
        self.assertEqual(record.probabilities.draw, 0.27)

    def test_same_fixture_can_have_distinct_rerun_records(self) -> None:
        first_data = prediction_record()
        second_data = prediction_record()
        second_data["prediction_id"] = "00000000-0000-4000-8000-000000000004"
        second_data["predicted_at"] = "2026-10-06T12:02:00Z"
        first = parse_prediction_record(first_data)
        second = parse_prediction_record(second_data)
        self.assertEqual(first.fixture_id, second.fixture_id)
        self.assertNotEqual(first.prediction_id, second.prediction_id)

    def test_rejects_missing_and_unexpected_top_level_fields(self) -> None:
        missing = prediction_record()
        del missing["probabilities"]
        with self.assertRaisesRegex(ValueError, "missing fields: probabilities"):
            parse_prediction_record(missing)
        extra = prediction_record()
        extra["actual_outcome"] = "HOME_WIN"
        with self.assertRaisesRegex(ValueError, "unexpected fields: actual_outcome"):
            parse_prediction_record(extra)

    def test_rejects_unknown_versions_and_malformed_identifiers(self) -> None:
        for field, value in (
            ("contract_version", "2.0"),
            ("input_schema_version", "features-v99"),
            ("fixture_id", "provider-event-123"),
        ):
            with self.subTest(field=field):
                data = prediction_record()
                data[field] = value
                with self.assertRaises(ValueError):
                    parse_prediction_record(data)

    def test_rejects_unrecognized_model_fields_and_empty_metadata(self) -> None:
        for mutation in (
            lambda model: model.update({"name": ""}),
            lambda model: model.update({"secret": "should-not-be-here"}),
        ):
            data = prediction_record()
            mutation(data["model"])
            with self.assertRaises(ValueError):
                parse_prediction_record(data)

    def test_requires_offset_and_prediction_strictly_before_kickoff(self) -> None:
        for predicted_at in (
            "2026-10-06T12:00:00",
            "2026-10-06T18:00:00Z",
            "2026-10-06T19:00:00+01:00",
        ):
            with self.subTest(predicted_at=predicted_at):
                data = prediction_record()
                data["predicted_at"] = predicted_at
                with self.assertRaises(ValueError):
                    parse_prediction_record(data)

    def test_probability_vector_requires_exact_keys_and_valid_values(self) -> None:
        invalid_vectors = (
            {"HOME_WIN": 0.5, "DRAW": 0.5},
            {"HOME_WIN": 0.5, "DRAW": 0.25, "AWAY_WIN": 0.25, "OTHER": 0.0},
            {"HOME_WIN": True, "DRAW": 0.0, "AWAY_WIN": 0.0},
            {"HOME_WIN": float("nan"), "DRAW": 0.0, "AWAY_WIN": 0.0},
            {"HOME_WIN": float("inf"), "DRAW": 0.0, "AWAY_WIN": 0.0},
            {"HOME_WIN": -0.01, "DRAW": 0.5, "AWAY_WIN": 0.51},
            {"HOME_WIN": 1.01, "DRAW": 0.0, "AWAY_WIN": 0.0},
        )
        for vector in invalid_vectors:
            with self.subTest(vector=vector):
                data = prediction_record()
                data["probabilities"] = vector
                with self.assertRaises(ValueError):
                    parse_prediction_record(data)

    def test_probability_sum_tolerance_is_absolute(self) -> None:
        data = prediction_record()
        data["probabilities"] = {
            "HOME_WIN": 0.48 + PROBABILITY_SUM_TOLERANCE / 4,
            "DRAW": 0.27,
            "AWAY_WIN": 0.25,
        }
        parse_prediction_record(data)
        data["probabilities"]["HOME_WIN"] = 0.48 + PROBABILITY_SUM_TOLERANCE * 2
        with self.assertRaisesRegex(ValueError, "sum to 1"):
            parse_prediction_record(data)

    def test_every_integer_grid_point_on_probability_simplex_is_accepted(self) -> None:
        # Exhaustively cover all three-way vectors in 0.01 increments.
        data = prediction_record()
        for home in range(101):
            for draw in range(101 - home):
                away = 100 - home - draw
                data["probabilities"] = {
                    "HOME_WIN": home / 100,
                    "DRAW": draw / 100,
                    "AWAY_WIN": away / 100,
                }
                with self.subTest(home=home, draw=draw, away=away):
                    record = parse_prediction_record(data)
                    self.assertIn(
                        record.probabilities.predicted_outcome,
                        ("HOME_WIN", "DRAW", "AWAY_WIN"),
                    )

    def test_hash_must_be_canonical_sha256_hex(self) -> None:
        for value in ("a" * 63, "A" * 64, "z" * 64):
            with self.subTest(value=value):
                data = prediction_record()
                data["input_snapshot_hash"] = value
                with self.assertRaisesRegex(ValueError, "64 lowercase hexadecimal"):
                    parse_prediction_record(data)


class PredictionFailureEventTests(unittest.TestCase):
    def test_valid_failure_event_is_structured_and_round_trips(self) -> None:
        event = parse_prediction_failure_event(failure_event())
        serialized = event.as_dict()
        self.assertEqual(serialized["event_type"], "prediction_failed")
        self.assertEqual(serialized["error_code"], "MODEL_TIMEOUT")
        self.assertTrue(serialized["retryable"])
        self.assertNotIn("message", serialized)
        self.assertEqual(parse_prediction_failure_event(serialized), event)

    def test_rejects_free_form_error_text_and_sensitive_extra_fields(self) -> None:
        for field, value in (
            ("error_code", "timeout: raw provider text"),
            ("error_code", "TOKEN_SECRET"),
            ("message", "raw API error"),
        ):
            with self.subTest(field=field):
                event = failure_event()
                event[field] = value
                with self.assertRaises(ValueError):
                    parse_prediction_failure_event(event)

    def test_rejects_bad_stage_type_and_post_kickoff_failure(self) -> None:
        invalid_events = (
            {**failure_event(), "stage": "UNKNOWN"},
            {**failure_event(), "stage": "PERSISTENCE", "error_code": "MODEL_TIMEOUT"},
            {**failure_event(), "retryable": 1},
            {**failure_event(), "occurred_at": "2026-10-06T18:00:00Z"},
        )
        for event in invalid_events:
            with self.subTest(event=event):
                with self.assertRaises(ValueError):
                    parse_prediction_failure_event(event)


if __name__ == "__main__":
    unittest.main()
