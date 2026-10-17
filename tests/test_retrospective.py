"""Offline tests for point-in-time retrospective cohorts and reports."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from proracle.contracts import parse_prediction_record
from proracle.evaluate import evaluate, load_predictions, load_training_results
from proracle.retrospective import (
    ResultVersion,
    StoredPrediction,
    build_cohort,
    build_report,
    current_training_results,
    export_jsonl,
    resolve_version,
    wilson_interval,
)
from proracle.supabase_adapter import SupabaseRESTStore

BASE = datetime(2030, 1, 1, tzinfo=timezone.utc)


def at(days: float, hours: float = 0) -> datetime:
    return BASE + timedelta(days=days, hours=hours)


def fid(number: int) -> str:
    return str(UUID(f"00000000-0000-4000-8000-{number:012d}"))


def prediction(
    number: int,
    *,
    predicted_at: datetime,
    kickoff_at: datetime,
    probabilities: tuple[float, float, float] = (0.5, 0.3, 0.2),
    model_version: str = "1",
    prediction_number: int | None = None,
    competition: str | None = "league-1",
) -> StoredPrediction:
    home, draw, away = probabilities
    record = parse_prediction_record(
        {
            "contract_version": "1.0",
            "prediction_id": fid(10_000 + (prediction_number or number)),
            "fixture_id": fid(number),
            "predicted_at": predicted_at.isoformat().replace("+00:00", "Z"),
            "kickoff_at": kickoff_at.isoformat().replace("+00:00", "Z"),
            "model": {"provider": "proracle", "name": "historical_frequency", "version": model_version},
            "prompt_version": "not_applicable",
            "input_schema_version": "features-v1",
            "input_snapshot_hash": "a" * 64,
            "probabilities": {"HOME_WIN": home, "DRAW": draw, "AWAY_WIN": away},
        }
    )
    return StoredPrediction(record, competition)


def version(
    number: int,
    *,
    kickoff_at: datetime,
    available_at: datetime,
    outcome: str | None = "HOME_WIN",
    status: str = "FINAL",
    result_number: int | None = None,
    supersedes: int | None = None,
) -> ResultVersion:
    return ResultVersion(
        result_id=fid(20_000 + (result_number or number)),
        fixture_id=fid(number),
        supersedes_id=fid(20_000 + supersedes) if supersedes else None,
        status=status,
        actual_outcome=outcome,
        kickoff_at=kickoff_at,
        available_at=available_at,
    )


def history() -> list[ResultVersion]:
    """Three earlier finals (days 0-2) so a baseline exists before the cohort starts."""
    return [
        version(1, kickoff_at=at(0), available_at=at(0, 3), outcome="HOME_WIN"),
        version(2, kickoff_at=at(1), available_at=at(1, 3), outcome="DRAW"),
        version(3, kickoff_at=at(2), available_at=at(2, 3), outcome="HOME_WIN"),
    ]


class ResultChainTests(unittest.TestCase):
    def test_correction_changes_current_label_but_not_what_was_known_earlier(self) -> None:
        original = version(1, kickoff_at=at(0), available_at=at(0, 3), outcome="HOME_WIN")
        correction = version(
            1,
            kickoff_at=at(0),
            available_at=at(5),
            outcome="AWAY_WIN",
            result_number=2,
            supersedes=1,
        )
        self.assertEqual(resolve_version([original, correction]).actual_outcome, "AWAY_WIN")
        as_known = resolve_version([original, correction], known_before=at(4))
        self.assertEqual(as_known.actual_outcome, "HOME_WIN")

    def test_void_correction_removes_the_earlier_final_label(self) -> None:
        original = version(1, kickoff_at=at(0), available_at=at(0, 3))
        void = version(
            1,
            kickoff_at=at(0),
            available_at=at(1),
            outcome=None,
            status="VOID",
            result_number=2,
            supersedes=1,
        )
        training, non_final = current_training_results([original, void])
        self.assertEqual(training, [])
        self.assertEqual(non_final, 1)

    def test_branching_chain_and_pre_kickoff_availability_are_rejected(self) -> None:
        first = version(1, kickoff_at=at(0), available_at=at(0, 3))
        branch_a = version(1, kickoff_at=at(0), available_at=at(1), result_number=2, supersedes=1)
        branch_b = version(1, kickoff_at=at(0), available_at=at(2), result_number=3, supersedes=1)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            resolve_version([first, branch_a, branch_b])
        early = version(1, kickoff_at=at(0), available_at=at(0) - timedelta(hours=1))
        with self.assertRaisesRegex(ValueError, "before kickoff"):
            resolve_version([early])


class CohortTests(unittest.TestCase):
    def cohort_inputs(self):
        predictions = [
            prediction(4, predicted_at=at(3), kickoff_at=at(3, 4), probabilities=(0.6, 0.25, 0.15)),
            prediction(5, predicted_at=at(3, 1), kickoff_at=at(3, 5), probabilities=(0.2, 0.3, 0.5)),
        ]
        versions = history() + [
            version(4, kickoff_at=at(3, 4), available_at=at(3, 8), outcome="HOME_WIN"),
            version(5, kickoff_at=at(3, 5), available_at=at(3, 9), outcome="AWAY_WIN"),
        ]
        return predictions, versions

    def test_training_is_known_strictly_before_every_scored_prediction(self) -> None:
        predictions, versions = self.cohort_inputs()
        cohort = build_cohort(predictions, versions)
        self.assertEqual({row.fixture_id for row in cohort.scored}, {fid(4), fid(5)})
        earliest = min(row.prediction_at for row in cohort.scored)
        self.assertEqual(len(cohort.training), 3)
        for row in cohort.training:
            self.assertLess(row.available_at, earliest)
        self.assertEqual(cohort.exclusions, {})

    def test_late_correction_cannot_change_a_past_training_label(self) -> None:
        predictions, versions = self.cohort_inputs()
        versions.append(
            version(
                1,
                kickoff_at=at(0),
                available_at=at(6),
                outcome="AWAY_WIN",
                result_number=99,
                supersedes=1,
            )
        )
        cohort = build_cohort(predictions, versions)
        labels = {row.fixture_id: row.actual_outcome for row in cohort.training}
        self.assertEqual(labels[fid(1)], "HOME_WIN")

    def test_reruns_use_the_earliest_prediction_and_are_counted(self) -> None:
        predictions, versions = self.cohort_inputs()
        predictions.append(
            prediction(
                4,
                predicted_at=at(3, 2),
                kickoff_at=at(3, 4),
                probabilities=(0.01, 0.01, 0.98),
                prediction_number=900,
            )
        )
        cohort = build_cohort(predictions, versions)
        scored = {row.fixture_id: row for row in cohort.scored}
        self.assertEqual(scored[fid(4)].probabilities["HOME_WIN"], 0.6)
        self.assertEqual(cohort.exclusions["ADDITIONAL_PREDICTION_NOT_SCORED"], 1)

    def test_non_final_missing_and_already_known_results_are_excluded_with_reasons(self) -> None:
        predictions = [
            prediction(4, predicted_at=at(3), kickoff_at=at(3, 4)),
            prediction(5, predicted_at=at(3), kickoff_at=at(3, 5)),
            prediction(6, predicted_at=at(3), kickoff_at=at(3, 6)),
            prediction(7, predicted_at=at(3), kickoff_at=at(3, 7)),
        ]
        versions = history() + [
            version(4, kickoff_at=at(3, 4), available_at=at(3, 8)),
            version(5, kickoff_at=at(3, 5), available_at=at(3, 9), outcome=None, status="CANCELLED"),
            # fixture 7 was rescheduled after prediction; its result was already known at prediction time
            version(7, kickoff_at=at(2), available_at=at(2, 2)),
        ]
        cohort = build_cohort(predictions, versions)
        self.assertEqual([row.fixture_id for row in cohort.scored], [fid(4)])
        self.assertEqual(
            dict(cohort.exclusions),
            {"NO_RESULT_YET": 1, "RESULT_CANCELLED": 1, "RESULT_KNOWN_BEFORE_PREDICTION": 1},
        )

    def test_model_identities_are_never_mixed(self) -> None:
        predictions, versions = self.cohort_inputs()
        predictions.append(
            prediction(4, predicted_at=at(3), kickoff_at=at(3, 4), model_version="2", prediction_number=901)
        )
        with self.assertRaisesRegex(ValueError, "more than one model identity"):
            build_cohort(predictions, versions)
        cohort = build_cohort(predictions, versions, model_version="1")
        self.assertEqual(cohort.exclusions["OTHER_MODEL_IDENTITY"], 1)
        with self.assertRaisesRegex(ValueError, "no predictions match"):
            build_cohort(predictions, versions, model_version="3")

    def test_window_selects_by_prediction_time(self) -> None:
        predictions, versions = self.cohort_inputs()
        cohort = build_cohort(predictions, versions, predicted_from=at(3, 0, ), predicted_until=at(3, 1))
        self.assertEqual([row.fixture_id for row in cohort.scored], [fid(4)])
        self.assertEqual(cohort.exclusions["OUTSIDE_WINDOW"], 1)

    def test_digest_changes_if_any_scored_probability_changes(self) -> None:
        predictions, versions = self.cohort_inputs()
        first = build_cohort(predictions, versions)
        again = build_cohort(predictions, versions)
        self.assertEqual(first.sha256, again.sha256)
        predictions[0] = prediction(
            4, predicted_at=at(3), kickoff_at=at(3, 4), probabilities=(0.61, 0.24, 0.15)
        )
        self.assertNotEqual(first.sha256, build_cohort(predictions, versions).sha256)


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.predictions = [
            prediction(4, predicted_at=at(3), kickoff_at=at(3, 4), probabilities=(0.6, 0.25, 0.15)),
            prediction(5, predicted_at=at(3, 1), kickoff_at=at(3, 5), probabilities=(0.2, 0.3, 0.5), competition="league-2"),
        ]
        self.versions = history() + [
            version(4, kickoff_at=at(3, 4), available_at=at(3, 8), outcome="HOME_WIN"),
            version(5, kickoff_at=at(3, 5), available_at=at(3, 9), outcome="AWAY_WIN"),
        ]

    def test_report_states_sample_size_uncertainty_slices_and_limits(self) -> None:
        report = build_report(build_cohort(self.predictions, self.versions))
        self.assertEqual(report["cohort"]["scored_fixtures"], 2)
        self.assertEqual(report["uncertainty"]["sample_size"], 2)
        self.assertTrue(report["uncertainty"]["descriptive_only"])
        accuracy = report["uncertainty"]["accuracy"]["model"]
        self.assertEqual(accuracy["estimate"], 1.0)
        self.assertLess(accuracy["lower"], 1.0)
        difference = report["uncertainty"]["paired_difference_model_minus_baseline"]["log_loss"]
        self.assertLess(difference["mean_difference"], 0)
        self.assertLessEqual(difference["lower"], difference["mean_difference"])
        self.assertGreaterEqual(difference["upper"], difference["mean_difference"])
        self.assertEqual(set(report["slices"]), {"actual_outcome", "competition", "kickoff_month"})
        self.assertEqual(report["slices"]["competition"]["league-2"]["fixtures"], 1)
        self.assertTrue(report["slices"]["actual_outcome"]["HOME_WIN"]["small_sample"])
        self.assertIn("no profitability claim", report["claim_limits"])
        self.assertEqual(report["training_fixtures"], 3)

    def test_report_is_deterministic(self) -> None:
        first = build_report(build_cohort(self.predictions, self.versions))
        second = build_report(build_cohort(self.predictions, self.versions))
        self.assertEqual(first, second)

    def test_no_scorable_prediction_fails_loudly(self) -> None:
        cohort = build_cohort(self.predictions, history())
        with self.assertRaisesRegex(ValueError, "no scorable"):
            build_report(cohort)

    def test_missing_training_history_fails_loudly(self) -> None:
        versions = self.versions[3:]
        with self.assertRaisesRegex(ValueError, "training result"):
            build_report(build_cohort(self.predictions, versions))

    def test_export_round_trips_through_the_existing_evaluator(self) -> None:
        cohort = build_cohort(self.predictions, self.versions)
        report = build_report(cohort)
        with tempfile.TemporaryDirectory() as directory:
            training_path, predictions_path = export_jsonl(cohort, Path(directory))
            replay = evaluate(
                load_training_results(training_path), load_predictions(predictions_path)
            )
        self.assertEqual(replay["model"], report["model"])
        self.assertEqual(replay["historical_frequency_baseline"], report["historical_frequency_baseline"])

    def test_wilson_interval_matches_reference_values(self) -> None:
        interval = wilson_interval(8, 10)
        self.assertAlmostEqual(interval["lower"], 0.4902, places=3)
        self.assertAlmostEqual(interval["upper"], 0.9433, places=3)
        with self.assertRaises(ValueError):
            wilson_interval(0, 0)


class StoreMappingTests(unittest.TestCase):
    def store(self, tables: dict[str, list[dict]]) -> SupabaseRESTStore:
        store = SupabaseRESTStore("https://project.example", "test-service-secret")
        store._request = lambda _method, table, **_kwargs: (tables[table], {})  # type: ignore[method-assign]
        return store

    def test_training_results_drop_a_final_label_later_voided(self) -> None:
        kickoff = {"kickoff_at": "2030-01-01T18:00:00Z"}
        store = self.store(
            {
                "results": [
                    {
                        "id": "r1", "fixture_id": "f1", "supersedes_result_id": None,
                        "status": "FINAL", "actual_outcome": "HOME_WIN",
                        "observed_at": "2030-01-01T21:00:00Z", "source_retrieved_at": None,
                        "fixture": kickoff,
                    },
                    {
                        "id": "r2", "fixture_id": "f1", "supersedes_result_id": "r1",
                        "status": "VOID", "actual_outcome": None,
                        "observed_at": "2030-01-02T21:00:00Z", "source_retrieved_at": None,
                        "fixture": kickoff,
                    },
                ]
            }
        )
        self.assertEqual(store.training_results(), [])
        self.assertEqual(len(store.result_versions()), 2)

    def test_stored_predictions_map_rows_into_validated_records(self) -> None:
        row = {
            "id": fid(1), "fixture_id": fid(2), "contract_version": "1.0",
            "predicted_at": "2030-01-01T10:00:00+00:00", "kickoff_at": "2030-01-01T18:00:00+00:00",
            "model_provider": "proracle", "model_name": "historical_frequency", "model_version": "1",
            "prompt_version": "not_applicable", "input_schema_version": "features-v1",
            "input_snapshot_hash": "b" * 64,
            "home_win_probability": 0.5, "draw_probability": 0.3, "away_win_probability": 0.2,
            "fixture": {"competition_id": "league-9"},
        }
        stored = self.store({"predictions": [row]}).stored_predictions()
        self.assertEqual(stored[0].competition_id, "league-9")
        self.assertEqual(stored[0].record.probabilities.draw, 0.3)

        bad = dict(row, home_win_probability=0.9)
        from proracle.supabase_adapter import SupabaseStoreError

        with self.assertRaises(SupabaseStoreError):
            self.store({"predictions": [bad]}).stored_predictions()


if __name__ == "__main__":
    unittest.main()
