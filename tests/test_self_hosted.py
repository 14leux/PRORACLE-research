"""Offline orchestration checks with fake provider and database boundaries."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from uuid import UUID

from proracle.bsd import BSDMatch, BSDSourceError
from proracle.evaluate import Result
from proracle.self_hosted import run_self_hosted


class FakeBSD:
    def __init__(self, upcoming: list[BSDMatch], finished: list[BSDMatch] | None = None):
        self.upcoming = upcoming
        self.finished = finished or []

    def fetch_events(self, status: str, _start: str, _end: str) -> list[BSDMatch]:
        if status == "upcoming":
            return self.upcoming
        return self.finished


class FakeStore:
    def __init__(self, training: list[Result]):
        self.training = training
        self.fixtures: dict[str, str] = {}
        self.predictions = []
        self.failures = []
        self.observed_times = []

    def ingest_match(self, match: BSDMatch, observed_at: datetime):
        self.observed_times.append(observed_at)
        fixture_id = self.fixtures.setdefault(match.source_event_key, f"00000000-0000-4000-8000-{int(match.source_event_key):012d}")
        return UUID(fixture_id), match.actual_outcome is not None

    def training_results(self):
        return self.training

    def append_prediction(self, record):
        self.predictions.append(record)

    def append_failure(self, event):
        self.failures.append(event)


def match(key: str, kickoff: str, *, status: str = "SCHEDULED") -> BSDMatch:
    return BSDMatch(
        source_event_key=key,
        home_team=f"Home {key}",
        away_team=f"Away {key}",
        kickoff_at=datetime.fromisoformat(kickoff.replace("Z", "+00:00")),
        competition_id=None,
        competition_name="Synthetic League",
        fixture_status=status,
        home_score=2 if status == "COMPLETED" else None,
        away_score=1 if status == "COMPLETED" else None,
    )


class SelfHostedCycleTests(unittest.TestCase):
    def test_execute_records_prediction_with_later_availability_and_prediction_times(self) -> None:
        upcoming = [match("10", "2030-01-05T18:00:00Z")]
        finished = [match("20", "2025-12-01T18:00:00Z", status="COMPLETED")]
        training = [
            Result(
                fixture_id="historical-1",
                kickoff_at=datetime(2025, 12, 1, 18, tzinfo=timezone.utc),
                actual_outcome="HOME_WIN",
                available_at=datetime(2025, 12, 1, 20, tzinfo=timezone.utc),
            )
        ]
        store = FakeStore(training)
        summary = run_self_hosted(
            execute=True,
            bsd_client=FakeBSD(upcoming, finished),
            store=store,
            now=datetime.now(timezone.utc),
        )
        self.assertEqual(summary["predictions_written"], 1)
        self.assertEqual(summary["fixtures_synced"], 2)
        self.assertEqual(summary["results_written"], 1)
        prediction = store.predictions[0]
        self.assertGreater(prediction.predicted_at, training[0].available_at)
        self.assertGreater(store.observed_times[0], finished[0].kickoff_at)
        self.assertLess(prediction.predicted_at, upcoming[0].kickoff_at)
        self.assertEqual(store.failures, [])

    def test_failed_finished_source_request_records_only_safe_prematch_failures(self) -> None:
        future = match("30", "2030-01-05T18:00:00Z")

        class FailingBSD(FakeBSD):
            def fetch_events(self, status: str, start: str, end: str):
                if status == "finished":
                    raise BSDSourceError("SOURCE_QUOTA_EXHAUSTED", retryable=False)
                return super().fetch_events(status, start, end)

        store = FakeStore([])
        summary = run_self_hosted(
            execute=True,
            bsd_client=FailingBSD([future]),
            store=store,
            now=datetime(2030, 1, 1, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["failures_written"], 1)
        event = store.failures[0]
        self.assertEqual(event.error_code, "SOURCE_QUOTA_EXHAUSTED")
        self.assertFalse(event.retryable)
        self.assertLess(event.occurred_at, event.kickoff_at)
        self.assertFalse(store.predictions)

    def test_dry_run_never_touches_store(self) -> None:
        class MustNotBeUsed:
            def __getattr__(self, _name: str):
                raise AssertionError("dry-run attempted a database operation")

        summary = run_self_hosted(
            execute=False,
            bsd_client=FakeBSD([match("40", "2030-01-05T18:00:00Z")]),
            store=MustNotBeUsed(),  # type: ignore[arg-type]
            now=datetime(2030, 1, 1, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["eligible_fixtures"], 1)
        self.assertEqual(summary["predictions_written"], 0)


if __name__ == "__main__":
    unittest.main()
