"""Scoring the rating identity against the frequency baseline on the same fixtures (synthetic)."""

from __future__ import annotations

import unittest
from datetime import timedelta
from uuid import UUID

from proracle.baselines import add_one_smoothed_probabilities
from proracle.contracts import parse_prediction_record
from proracle.domain import normalize_fixture
from proracle.rating import (
    RATING_MODEL_NAME,
    build_rating_state,
    create_rating_prediction,
)
from proracle.retrospective import (
    ResultVersion,
    StoredPrediction,
    build_cohort,
    build_paired_comparison,
    build_report,
)
from tests.test_rating import fid, synthetic_league


def stored(record) -> StoredPrediction:
    return StoredPrediction(record, "synthetic-league")


def frequency_record(item, predicted_at, probabilities, *, prediction_number):
    return parse_prediction_record(
        {
            "contract_version": "1.0",
            "prediction_id": fid(prediction_number),
            "fixture_id": item.fixture_id,
            "predicted_at": predicted_at.isoformat().replace("+00:00", "Z"),
            "kickoff_at": item.kickoff_at.isoformat().replace("+00:00", "Z"),
            "model": {"provider": "proracle", "name": "historical_frequency", "version": "1"},
            "prompt_version": "not_applicable",
            "input_schema_version": "features-v1",
            "input_snapshot_hash": "b" * 64,
            "probabilities": probabilities,
        }
    )


class RatingVersusFrequencyTests(unittest.TestCase):
    """A walk-forward run over a fabricated league, scored with the retrospective tools."""

    @classmethod
    def setUpClass(cls) -> None:
        league = synthetic_league()
        cls.versions = [
            ResultVersion(
                result_id=fid(800_000 + n),
                fixture_id=item.fixture_id,
                supersedes_id=None,
                status="FINAL",
                actual_outcome=item.actual_outcome,
                kickoff_at=item.kickoff_at,
                available_at=item.available_at,
            )
            for n, item in enumerate(league)
        ]
        cls.predictions = []
        number = 900_000
        for item in league[len(league) // 2 :: 3]:
            predicted_at = item.kickoff_at - timedelta(hours=20)
            visible = [r.actual_outcome for r in league if r.available_at < predicted_at]
            number += 1
            cls.predictions.append(
                stored(
                    frequency_record(
                        item,
                        predicted_at,
                        add_one_smoothed_probabilities(visible),
                        prediction_number=number,
                    )
                )
            )
            fixture = normalize_fixture(
                {
                    "fixture_id": item.fixture_id,
                    "home_team": item.home_team,
                    "away_team": item.away_team,
                    "kickoff_at": item.kickoff_at.isoformat(),
                }
            )
            number += 1
            cls.predictions.append(
                stored(
                    create_rating_prediction(
                        fixture,
                        build_rating_state(league, predicted_at),
                        prediction_id=UUID(fid(number)),
                        predicted_at=predicted_at,
                    )
                )
            )

    def _compare(self, predictions, **kwargs):
        options = {"candidate_name": RATING_MODEL_NAME, "reference_name": "historical_frequency"}
        options.update(kwargs)
        return build_paired_comparison(predictions, self.versions, **options)

    def test_two_identities_are_never_mixed_in_one_cohort(self) -> None:
        with self.assertRaises(ValueError):
            build_cohort(self.predictions, self.versions)
        cohort = build_cohort(self.predictions, self.versions, model_name=RATING_MODEL_NAME)
        self.assertEqual(cohort.model_identity["input_schema_version"], "results-v1")
        report = build_report(cohort)
        half = len(self.predictions) // 2
        self.assertEqual(report["cohort"]["scored_fixtures"], half)
        self.assertEqual(report["exclusions"]["OTHER_MODEL_IDENTITY"], half)

    def test_paired_comparison_scores_rating_against_frequency_on_the_same_fixtures(self) -> None:
        report = self._compare(self.predictions)
        self.assertEqual(report["paired_fixtures"], len(self.predictions) // 2)
        self.assertEqual(report["candidate"]["unpaired_fixtures"], 0)
        self.assertEqual(report["same_prediction_time_share"], 1.0)
        diff = report["paired_difference_candidate_minus_reference"]["log_loss"]
        self.assertLessEqual(diff["lower"], diff["mean_difference"])
        self.assertLessEqual(diff["mean_difference"], diff["upper"])
        self.assertAlmostEqual(
            diff["mean_difference"],
            report["candidate"]["paired"]["log_loss"] - report["reference"]["paired"]["log_loss"],
            places=12,
        )
        # Fabricated strengths are wide, so the rating should be ahead here (tooling check only).
        self.assertLess(diff["mean_difference"], 0.0)
        for arm in ("candidate", "reference"):
            gap = report[arm]["draw_calibration"]
            self.assertAlmostEqual(
                gap["absolute_gap"], abs(gap["mean_draw_probability"] - gap["observed_draw_rate"])
            )
        self.assertEqual(
            sum(s["fixtures"] for s in report["slices"]["actual_outcome"].values()),
            report["paired_fixtures"],
        )
        self.assertEqual(report["sha256"], self._compare(list(reversed(self.predictions)))["sha256"])

    def test_earliest_prediction_per_identity_is_scored_and_unpaired_fixtures_are_counted(self) -> None:
        first = self.predictions[1].record  # a rating prediction
        rerun = parse_prediction_record(
            {
                **first.as_dict(),
                "prediction_id": fid(999_001),
                "predicted_at": (first.predicted_at + timedelta(hours=1)).isoformat(),
                "probabilities": {"HOME_WIN": 0.5, "DRAW": 0.25, "AWAY_WIN": 0.25},
            }
        )
        dropped_reference = [p for p in self.predictions if p is not self.predictions[2]]
        report = self._compare([*dropped_reference, stored(rerun)])
        self.assertEqual(report["candidate"]["exclusions"]["ADDITIONAL_PREDICTION_NOT_SCORED"], 1)
        self.assertEqual(report["candidate"]["unpaired_fixtures"], 1)
        self.assertEqual(report["paired_fixtures"], len(self.predictions) // 2 - 1)
        baseline = self._compare(self.predictions)
        self.assertEqual(
            report["candidate"]["scored_fixtures"], baseline["candidate"]["scored_fixtures"]
        )

    def test_same_identity_or_no_overlap_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._compare(self.predictions, reference_name=RATING_MODEL_NAME)
        rating_only = [p for p in self.predictions if p.record.model.name == RATING_MODEL_NAME]
        freq_only = [p for p in self.predictions if p.record.model.name != RATING_MODEL_NAME]
        with self.assertRaises(ValueError):
            self._compare(rating_only[:3] + freq_only[3:])


if __name__ == "__main__":
    unittest.main()
