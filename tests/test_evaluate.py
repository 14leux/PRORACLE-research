import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from proracle.evaluate import (
    Prediction,
    Result,
    evaluate,
    load_predictions,
    load_training_results,
)

ROOT = Path(__file__).resolve().parents[1]


class EvaluateTests(unittest.TestCase):
    def test_synthetic_example_scores_candidate_and_frequency_baseline(self):
        training = load_training_results(ROOT / "data/synthetic/training_results.jsonl")
        predictions = load_predictions(ROOT / "data/synthetic/predictions.jsonl")

        report = evaluate(training, predictions)

        self.assertEqual(report["training_fixtures"], 8)
        self.assertEqual(report["evaluation_fixtures"], 6)
        self.assertEqual(report["model"]["accuracy"], 1.0)
        self.assertEqual(report["historical_frequency_baseline"]["accuracy"], 2 / 6)
        self.assertAlmostEqual(report["model"]["brier_score"], 0.4091666667)
        self.assertAlmostEqual(report["model"]["log_loss"], 0.7290097442)
        self.assertGreaterEqual(
            report["model"]["classwise_expected_calibration_error"], 0.0
        )
        self.assertLessEqual(
            report["model"]["classwise_expected_calibration_error"], 1.0
        )
        self.assertAlmostEqual(
            report["historical_frequency_baseline_probabilities"]["HOME_WIN"], 5 / 11
        )

    def test_rejects_zero_probabilities_total(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "predictions.jsonl"
            row = {
                "fixture_id": "bad-1",
                "prediction_at": "2026-02-01T10:00:00Z",
                "kickoff_at": "2026-02-01T15:00:00Z",
                "actual_outcome": "HOME_WIN",
                "probabilities": {"HOME_WIN": 0.2, "DRAW": 0.2, "AWAY_WIN": 0.2},
            }
            path.write_text(json.dumps(row), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sum to 1"):
                load_predictions(path)

    def test_rejects_prediction_at_or_after_kickoff(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "predictions.jsonl"
            row = {
                "fixture_id": "late-1",
                "prediction_at": "2026-02-01T15:00:00Z",
                "kickoff_at": "2026-02-01T15:00:00Z",
                "actual_outcome": "HOME_WIN",
                "probabilities": {"HOME_WIN": 0.5, "DRAW": 0.3, "AWAY_WIN": 0.2},
            }
            path.write_text(json.dumps(row), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "before kickoff"):
                load_predictions(path)

    def test_rejects_naive_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "training.jsonl"
            row = {
                "fixture_id": "naive-1",
                "kickoff_at": "2026-01-01T15:00:00",
                "actual_outcome": "HOME_WIN",
            }
            path.write_text(json.dumps(row), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "UTC offset"):
                load_training_results(path)

    def test_rejects_training_outcome_not_available_before_prediction(self):
        training = [
            Result("train", datetime(2026, 2, 1, 12, tzinfo=timezone.utc), "HOME_WIN")
        ]
        predictions = [
            Prediction(
                "test",
                datetime(2026, 2, 1, 16, tzinfo=timezone.utc),
                "HOME_WIN",
                datetime(2026, 2, 1, 11, tzinfo=timezone.utc),
                {"HOME_WIN": 0.5, "DRAW": 0.3, "AWAY_WIN": 0.2},
            )
        ]
        with self.assertRaisesRegex(ValueError, "training results must be final"):
            evaluate(training, predictions)

    def test_uses_explicit_result_availability_time_for_temporal_guard(self):
        training = [
            Result(
                "train",
                datetime(2026, 2, 1, 12, tzinfo=timezone.utc),
                "HOME_WIN",
                available_at=datetime(2026, 2, 2, 12, tzinfo=timezone.utc),
            )
        ]
        predictions = [
            Prediction(
                "test",
                datetime(2026, 2, 2, 16, tzinfo=timezone.utc),
                "HOME_WIN",
                datetime(2026, 2, 2, 11, tzinfo=timezone.utc),
                {"HOME_WIN": 0.5, "DRAW": 0.3, "AWAY_WIN": 0.2},
            )
        ]
        with self.assertRaisesRegex(ValueError, "training results must be final"):
            evaluate(training, predictions)

    def test_rejects_empty_partitions(self):
        with self.assertRaisesRegex(ValueError, "training result"):
            evaluate([], [])
        training = [
            Result("train", datetime(2026, 1, 1, tzinfo=timezone.utc), "HOME_WIN")
        ]
        with self.assertRaisesRegex(ValueError, "at least one prediction"):
            evaluate(training, [])


if __name__ == "__main__":
    unittest.main()
