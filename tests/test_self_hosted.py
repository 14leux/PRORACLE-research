"""Offline orchestration checks with fake provider and database boundaries."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from uuid import UUID

from proracle.bsd import BSDMatch, BSDSourceError
from proracle.evaluate import Result
from unittest.mock import patch

from proracle.self_hosted import main, run_self_hosted
from proracle.supabase_adapter import SupabaseStoreError


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
            now=datetime(2030, 1, 1, 12, tzinfo=timezone.utc),
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


class ClockTests(unittest.TestCase):
    """A pinned `now` must pin every timestamp in the cycle (Lesson 169)."""

    def _training(self):
        return [
            Result(
                fixture_id="h1",
                kickoff_at=datetime(2025, 12, 1, 18, tzinfo=timezone.utc),
                actual_outcome="HOME_WIN",
                available_at=datetime(2025, 12, 1, 20, tzinfo=timezone.utc),
            )
        ]

    def test_pinned_now_stamps_retrieval_and_prediction_and_ignores_the_wall_clock(self) -> None:
        pinned = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        store = FakeStore(self._training())
        run_self_hosted(
            execute=True,
            bsd_client=FakeBSD(
                [match("10", "2030-01-05T18:00:00Z")],
                [match("20", "2025-12-01T18:00:00Z", status="COMPLETED")],
            ),
            store=store,
            now=pinned,
        )
        self.assertEqual(store.predictions[0].predicted_at, pinned)
        self.assertEqual(store.observed_times, [pinned, pinned])

    def test_explicit_clock_is_used_for_every_reading(self) -> None:
        ticks = iter(
            datetime(2030, 1, 1, 12, minute, tzinfo=timezone.utc) for minute in range(10)
        )
        store = FakeStore(self._training())
        run_self_hosted(
            execute=True,
            bsd_client=FakeBSD([match("10", "2030-01-05T18:00:00Z")], []),
            store=store,
            clock=lambda: next(ticks),
        )
        # start of cycle (minute 0), retrieved_at (1), predicted_at (2)
        self.assertEqual(store.predictions[0].predicted_at.minute, 2)


class FlakyStore(FakeStore):
    """Fails the write for chosen source event keys, like a transient database error."""

    def __init__(self, training, bad_keys):
        super().__init__(training)
        self.bad_keys = set(bad_keys)

    def ingest_match(self, match, observed_at):
        if match.source_event_key in self.bad_keys:
            raise SupabaseStoreError()
        return super().ingest_match(match, observed_at)


class IngestIsolationTests(unittest.TestCase):
    def test_one_failed_finished_write_does_not_abandon_the_window(self) -> None:
        upcoming = [match("10", "2030-01-05T18:00:00Z")]
        finished = [
            match("20", "2025-12-01T18:00:00Z", status="COMPLETED"),
            match("21", "2025-12-02T18:00:00Z", status="COMPLETED"),
        ]
        training = [
            Result(
                fixture_id="h1",
                kickoff_at=datetime(2025, 12, 1, 18, tzinfo=timezone.utc),
                actual_outcome="HOME_WIN",
                available_at=datetime(2025, 12, 1, 20, tzinfo=timezone.utc),
            )
        ]
        store = FlakyStore(training, bad_keys={"20"})
        summary = run_self_hosted(
            execute=True,
            bsd_client=FakeBSD(upcoming, finished),
            store=store,
            now=datetime(2030, 1, 1, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["ingest_errors"], 1)
        self.assertEqual(summary["fixtures_synced"], 2)
        self.assertEqual(summary["predictions_written"], 1)

    def test_failed_upcoming_write_skips_only_that_fixture(self) -> None:
        upcoming = [match("10", "2030-01-05T18:00:00Z"), match("11", "2030-01-05T19:00:00Z")]
        training = [
            Result(
                fixture_id="h1",
                kickoff_at=datetime(2025, 12, 1, 18, tzinfo=timezone.utc),
                actual_outcome="HOME_WIN",
                available_at=datetime(2025, 12, 1, 20, tzinfo=timezone.utc),
            )
        ]
        store = FlakyStore(training, bad_keys={"10"})
        summary = run_self_hosted(
            execute=True,
            bsd_client=FakeBSD(upcoming, []),
            store=store,
            now=datetime(2030, 1, 1, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["ingest_errors"], 1)
        self.assertEqual(summary["predictions_written"], 1)
        self.assertEqual(len(store.predictions), 1)

    def test_main_exits_nonzero_when_any_write_was_missed(self) -> None:
        with patch("proracle.self_hosted.run_self_hosted", return_value={"ingest_errors": 1}):
            self.assertEqual(main(["--execute"]), 3)
        with patch("proracle.self_hosted.run_self_hosted", return_value={"ingest_errors": 0}):
            self.assertEqual(main(["--execute"]), 0)


from proracle.rating import RATING_MODEL_NAME, RatingResult  # noqa: E402


def _history() -> list[RatingResult]:
    rows = []
    for n in range(1, 13):
        kickoff = datetime(2029, 12, 1, 18, tzinfo=timezone.utc).replace(day=n)
        rows.append(
            RatingResult(
                fixture_id=f"h{n}",
                home_team="Home 10" if n % 2 else "Away 10",
                away_team="Away 10" if n % 2 else "Home 10",
                kickoff_at=kickoff,
                available_at=kickoff.replace(hour=20),
                actual_outcome="HOME_WIN" if n % 3 else "DRAW",
            )
        )
    return rows


class RatingStore(FakeStore):
    def __init__(self, training, history=None, *, fail_read=False, fail_rating_write=False):
        super().__init__(training)
        self.history = _history() if history is None else history
        self.fail_read = fail_read
        self.fail_rating_write = fail_rating_write

    def rating_results(self):
        if self.fail_read:
            raise SupabaseStoreError()
        return self.history

    def append_prediction(self, record):
        if self.fail_rating_write and record.model.name == RATING_MODEL_NAME:
            raise SupabaseStoreError()
        super().append_prediction(record)


class RatingIdentityTests(unittest.TestCase):
    NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)

    def _training(self):
        return [
            Result(
                fixture_id="h1",
                kickoff_at=datetime(2025, 12, 1, 18, tzinfo=timezone.utc),
                actual_outcome="HOME_WIN",
                available_at=datetime(2025, 12, 1, 20, tzinfo=timezone.utc),
            )
        ]

    def _run(self, store):
        return run_self_hosted(
            execute=True,
            bsd_client=FakeBSD([match("10", "2030-01-05T18:00:00Z"), match("11", "2030-01-06T18:00:00Z")]),
            store=store,
            now=self.NOW,
        )

    def _baseline(self, store):
        return [p for p in store.predictions if p.model.name == "historical_frequency"]

    def test_each_fixture_gets_a_baseline_and_a_distinct_rating_prediction(self) -> None:
        store = RatingStore(self._training())
        summary = self._run(store)
        self.assertEqual(summary["predictions_written"], 2)
        self.assertEqual(summary["rating_predictions_written"], 2)
        self.assertEqual(summary["rating_failures"], 0)
        self.assertEqual(summary["rating_history_results"], 12)
        rating = [p for p in store.predictions if p.model.name == RATING_MODEL_NAME]
        baseline = self._baseline(store)
        self.assertEqual(len(rating), 2)
        self.assertEqual(len(baseline), 2)
        for r, b in zip(rating, baseline):
            self.assertEqual(r.fixture_id, b.fixture_id)
            self.assertEqual(r.predicted_at, b.predicted_at)
            self.assertEqual(r.predicted_at, self.NOW)
            self.assertNotEqual(
                (r.model.name, r.input_schema_version, r.input_snapshot_hash),
                (b.model.name, b.input_schema_version, b.input_snapshot_hash),
            )
            self.assertEqual(r.input_schema_version, "results-v1")
            self.assertNotEqual(r.prediction_id, b.prediction_id)
        self.assertEqual(store.failures, [])

    def test_rating_failures_never_block_or_alter_the_baseline(self) -> None:
        reference = RatingStore(self._training())
        self._run(reference)
        cases = {
            "read fails": RatingStore(self._training(), fail_read=True),
            "write fails": RatingStore(self._training(), fail_rating_write=True),
            "no visible history": RatingStore(self._training(), history=[]),
            "store has no rating read": FakeStore(self._training()),
        }
        for label, store in cases.items():
            with self.subTest(label):
                summary = self._run(store)
                self.assertEqual(summary["predictions_written"], 2)
                self.assertEqual(summary["rating_predictions_written"], 0)
                self.assertEqual(summary["rating_failures"], 2)
                self.assertEqual(summary["ingest_errors"], 0)
                # No failure event is written for the rating identity (no identity column yet).
                self.assertEqual(store.failures, [])
                self.assertEqual(
                    [p.probabilities for p in self._baseline(store)],
                    [p.probabilities for p in self._baseline(reference)],
                )
                self.assertEqual(
                    [p.input_snapshot_hash for p in self._baseline(store)],
                    [p.input_snapshot_hash for p in self._baseline(reference)],
                )

    def test_baseline_failure_does_not_stop_the_rating_identity(self) -> None:
        store = RatingStore([])
        summary = self._run(store)
        self.assertEqual(summary["predictions_written"], 0)
        self.assertEqual(summary["failures_written"], 2)
        self.assertEqual(summary["rating_predictions_written"], 2)

    def test_rating_uses_only_history_available_before_the_pinned_prediction_time(self) -> None:
        late = RatingResult(
            fixture_id="late",
            home_team="Home 10",
            away_team="Away 10",
            kickoff_at=datetime(2030, 1, 1, 9, tzinfo=timezone.utc),
            available_at=datetime(2030, 1, 1, 12, 0, 1, tzinfo=timezone.utc),
            actual_outcome="AWAY_WIN",
        )
        plain, with_late = RatingStore(self._training()), RatingStore(self._training(), history=[*_history(), late])
        self._run(plain)
        self._run(with_late)
        pick = lambda s: [p.probabilities for p in s.predictions if p.model.name == RATING_MODEL_NAME]  # noqa: E731
        self.assertEqual(pick(plain), pick(with_late))

    def test_dry_run_reports_zero_rating_counters(self) -> None:
        summary = run_self_hosted(
            execute=False,
            bsd_client=FakeBSD([match("40", "2030-01-05T18:00:00Z")]),
            now=self.NOW,
        )
        self.assertEqual(summary["rating_predictions_written"], 0)
        self.assertEqual(summary["rating_failures"], 0)


class SourceInvalidRowsSummaryTests(unittest.TestCase):
    def test_summary_carries_the_skipped_row_count_and_the_run_still_succeeds(self) -> None:
        class SkippingBSD(FakeBSD):
            invalid_rows_skipped = 2

        now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        summary = run_self_hosted(
            execute=False, bsd_client=SkippingBSD([match("1", "2030-01-02T18:00:00Z")]), now=now
        )
        self.assertEqual(summary["source_invalid_rows"], 2)
        with patch("proracle.self_hosted.run_self_hosted", return_value=summary):
            self.assertEqual(main([]), 0)

    def test_summary_reports_zero_when_nothing_was_skipped(self) -> None:
        now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
        summary = run_self_hosted(
            execute=False, bsd_client=FakeBSD([match("1", "2030-01-02T18:00:00Z")]), now=now
        )
        self.assertEqual(summary["source_invalid_rows"], 0)


if __name__ == "__main__":
    unittest.main()


class CompetitionNameTests(unittest.TestCase):
    NOW = datetime(2030, 1, 1, 10, tzinfo=timezone.utc)

    def labelled(self, key: str, league_id: str | None, name: str | None) -> BSDMatch:
        from dataclasses import replace

        return replace(
            match(key, "2030-01-01T18:00:00Z"), competition_id=league_id, competition_name=name
        )

    def run_cycle(self, bsd) -> tuple[dict[str, int], list[BSDMatch]]:
        seen: list[BSDMatch] = []

        class Recording(FakeStore):
            def ingest_match(self, match: BSDMatch, observed_at: datetime):
                seen.append(match)
                return super().ingest_match(match, observed_at)

        summary = run_self_hosted(execute=True, bsd_client=bsd, store=Recording([]), now=self.NOW)
        return summary, seen

    def test_missing_names_are_filled_from_the_league_map_and_existing_names_are_kept(self) -> None:
        class NamedBSD(FakeBSD):
            def fetch_league_names(self):
                return {"9": "Synthetic League Nine", "5": "Never Used"}

        upcoming = [
            self.labelled("1", "9", None),
            self.labelled("2", "5", "Name From The Event"),
            self.labelled("3", "7", None),  # id not in the map: stays unnamed
            self.labelled("4", None, None),
        ]
        summary, seen = self.run_cycle(NamedBSD(upcoming))
        names = {m.source_event_key: m.competition_name for m in seen}
        self.assertEqual(
            names, {"1": "Synthetic League Nine", "2": "Name From The Event", "3": None, "4": None}
        )
        self.assertEqual(summary["competition_names_filled"], 2)

    def test_a_league_lookup_failure_never_blocks_the_cycle(self) -> None:
        class FailingNames(FakeBSD):
            def fetch_league_names(self):
                raise BSDSourceError("SOURCE_UNAVAILABLE", retryable=True)

        summary, seen = self.run_cycle(FailingNames([self.labelled("1", "9", None)]))
        self.assertEqual(summary["fixtures_synced"], 1)
        self.assertEqual(summary["ingest_errors"], 0)
        self.assertEqual([m.competition_name for m in seen], [None])
        self.assertEqual(summary["competition_names_filled"], 0)

    def test_a_client_without_the_lookup_is_unchanged(self) -> None:
        summary, seen = self.run_cycle(FakeBSD([self.labelled("1", "9", None)]))
        self.assertEqual([m.competition_name for m in seen], [None])
        self.assertEqual(summary["fixtures_synced"], 1)
