"""Offline tests for fixture, feature, prediction, and result domain contracts."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from uuid import UUID

from proracle.domain import (
    attach_final_result,
    create_baseline_prediction,
    normalize_fixture,
    validate_features,
)
from proracle.evaluate import Result

FIXTURE_ID = "00000000-0000-4000-8000-000000000201"
PREDICTION_ID = UUID("00000000-0000-4000-8000-000000000401")
KICKOFF = "2026-02-01T15:00:00Z"
PREDICTION_AT = datetime(2026, 2, 1, 12, tzinfo=timezone.utc)


def fixture(**updates):
    row = {
        "fixture_id": FIXTURE_ID,
        "home_team": "  Synthetic   Home ",
        "away_team": "Synthetic Away",
        "kickoff_at": KICKOFF,
    }
    row.update(updates)
    return row


def history():
    return [
        Result(
            "prior-home",
            datetime(2026, 1, 1, 15, tzinfo=timezone.utc),
            "HOME_WIN",
            available_at=datetime(2026, 1, 1, 18, tzinfo=timezone.utc),
        ),
        Result(
            "prior-draw",
            datetime(2026, 1, 2, 15, tzinfo=timezone.utc),
            "DRAW",
            available_at=datetime(2026, 1, 2, 18, tzinfo=timezone.utc),
        ),
    ]


class FixtureNormalizationTests(unittest.TestCase):
    def test_normalizes_names_and_utc_timestamp(self):
        normalized = normalize_fixture(fixture(kickoff_at="2026-02-01T18:00:00+03:00"))
        self.assertEqual(normalized.fixture_id, UUID(FIXTURE_ID))
        self.assertEqual(normalized.home_team, "Synthetic Home")
        self.assertEqual(normalized.kickoff_at, datetime(2026, 2, 1, 15, tzinfo=timezone.utc))

    def test_rejects_extra_fields_missing_values_and_same_teams(self):
        invalid = (
            fixture(raw_provider_response={"private": "not copied"}),
            {"fixture_id": FIXTURE_ID, "home_team": "Home", "kickoff_at": KICKOFF},
            fixture(away_team="  synthetic home "),
            fixture(kickoff_at="2026-02-01T15:00:00"),
            fixture(fixture_id="provider-id-123"),
        )
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                normalize_fixture(row)


class FeatureValidationTests(unittest.TestCase):
    def test_hash_is_stable_across_key_order_and_snapshot_is_copied(self):
        first = validate_features({"home_form_index": 0.7, "away_form_index": 0.4})
        second = validate_features({"away_form_index": 0.4, "home_form_index": 0.7})
        self.assertEqual(first.sha256, second.sha256)
        snapshot = first.as_dict()
        snapshot["home_form_index"] = 0.0
        self.assertEqual(first.as_dict()["home_form_index"], 0.7)

    def test_rejects_empty_bad_names_non_numeric_and_non_finite_values(self):
        for values in (
            {},
            {"Bad Name": 1.0},
            {"form": True},
            {"form": float("nan")},
            {"form": float("inf")},
            {"form": "0.5"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate_features(values)


class BaselinePredictionTests(unittest.TestCase):
    def test_uses_only_results_available_before_prediction_time(self):
        training = history() + [
            Result(
                "future-result",
                datetime(2026, 2, 1, 10, tzinfo=timezone.utc),
                "AWAY_WIN",
                available_at=datetime(2026, 2, 1, 13, tzinfo=timezone.utc),
            )
        ]
        record = create_baseline_prediction(
            normalize_fixture(fixture()),
            validate_features({"home_form_index": 0.7}),
            training,
            prediction_id=PREDICTION_ID,
            predicted_at=PREDICTION_AT,
        )
        self.assertEqual(record.probabilities.as_dict(), {"HOME_WIN": 0.4, "DRAW": 0.4, "AWAY_WIN": 0.2})
        self.assertEqual(record.model.name, "historical_frequency")
        self.assertEqual(record.prompt_version, "not_applicable")
        self.assertEqual(record.predicted_at, PREDICTION_AT)

    def test_requires_prior_history_and_prematch_time(self):
        normalized = normalize_fixture(fixture())
        features = validate_features({"home_form_index": 0.7})
        future_only = [
            Result(
                "future",
                datetime(2026, 2, 1, 9, tzinfo=timezone.utc),
                "HOME_WIN",
                available_at=datetime(2026, 2, 1, 13, tzinfo=timezone.utc),
            )
        ]
        with self.assertRaisesRegex(ValueError, "no historical outcomes"):
            create_baseline_prediction(
                normalized,
                features,
                future_only,
                prediction_id=PREDICTION_ID,
                predicted_at=PREDICTION_AT,
            )
        with self.assertRaisesRegex(ValueError, "before kickoff"):
            create_baseline_prediction(
                normalized,
                features,
                history(),
                prediction_id=PREDICTION_ID,
                predicted_at=datetime(2026, 2, 1, 15, tzinfo=timezone.utc),
            )


class FinalResultJoinTests(unittest.TestCase):
    def test_exports_existing_evaluator_row_shape_after_finalization(self):
        prediction = create_baseline_prediction(
            normalize_fixture(fixture()),
            validate_features({"home_form_index": 0.7}),
            history(),
            prediction_id=PREDICTION_ID,
            predicted_at=PREDICTION_AT,
        )
        row = attach_final_result(
            prediction,
            {
                "id": "00000000-0000-4000-8000-000000000301",
                "fixture_id": FIXTURE_ID,
                "status": "FINAL",
                "actual_outcome": "HOME_WIN",
                "finalized_at": "2026-02-01T17:00:00Z",
            },
        )
        self.assertEqual(
            set(row.as_dict()),
            {"fixture_id", "kickoff_at", "prediction_at", "actual_outcome", "probabilities"},
        )
        self.assertEqual(row.as_dict()["actual_outcome"], "HOME_WIN")

    def test_rejects_mismatched_nonfinal_or_invalid_result(self):
        prediction = create_baseline_prediction(
            normalize_fixture(fixture()),
            validate_features({"home_form_index": 0.7}),
            history(),
            prediction_id=PREDICTION_ID,
            predicted_at=PREDICTION_AT,
        )
        base = {
            "fixture_id": FIXTURE_ID,
            "status": "FINAL",
            "actual_outcome": "HOME_WIN",
            "finalized_at": "2026-02-01T17:00:00Z",
        }
        for changed in (
            {**base, "fixture_id": "00000000-0000-4000-8000-000000000202"},
            {**base, "status": "ABANDONED", "actual_outcome": None},
            {**base, "actual_outcome": "UNKNOWN"},
            {**base, "finalized_at": "2026-02-01T14:00:00Z"},
            {**base, "provider_payload": {"raw": True}},
        ):
            with self.subTest(result=changed), self.assertRaises(ValueError):
                attach_final_result(prediction, changed)


if __name__ == "__main__":
    unittest.main()
