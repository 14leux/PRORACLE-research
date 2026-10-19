"""Run one operator's local BSD-to-Supabase prediction cycle."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from proracle.bsd import BSDClient, BSDMatch, BSDSourceError
from proracle.contracts import PredictionFailureEvent
from proracle.domain import create_baseline_prediction, validate_features
from proracle.evaluate import Result
from proracle.rating import RatingState, build_rating_state, create_rating_prediction
from proracle.supabase_adapter import SupabaseRESTStore, SupabaseStoreError


def _positive_days(variable: str, default: int, maximum: int) -> int:
    raw = os.environ.get(variable, str(default))
    try:
        parsed = int(raw)
    except ValueError as error:
        raise ValueError(f"{variable} must be an integer from 1 to {maximum}") from error
    if not 1 <= parsed <= maximum:
        raise ValueError(f"{variable} must be an integer from 1 to {maximum}")
    return parsed


def _failure_event(
    fixture_id: str,
    kickoff_at: datetime,
    *,
    code: str,
    stage: str,
    retryable: bool,
    occurred_at: datetime,
) -> PredictionFailureEvent:
    return PredictionFailureEvent(
        contract_version="1.0",
        event_id=uuid4(),
        fixture_id=UUID(fixture_id),
        occurred_at=occurred_at,
        kickoff_at=kickoff_at,
        stage=stage,
        error_code=code,
        retryable=retryable,
    )


def _outcome_counts(results: list[Result], predicted_at: datetime) -> dict[str, float]:
    eligible = [
        result
        for result in results
        if (result.available_at or result.kickoff_at).astimezone(timezone.utc)
        < predicted_at.astimezone(timezone.utc)
    ]
    counts = Counter(result.actual_outcome for result in eligible)
    return {
        "home_win_count": float(counts.get("HOME_WIN", 0)),
        "draw_count": float(counts.get("DRAW", 0)),
        "away_win_count": float(counts.get("AWAY_WIN", 0)),
    }


def _environment_client() -> BSDClient:
    token = os.environ.get("BSD_API_TOKEN", "")
    if not token:
        raise ValueError("BSD_API_TOKEN is required")
    # A 30-day history page is large; allow the provider 30 s per read and one more retry.
    return BSDClient(token, timeout_seconds=30.0, max_retries=2)


def run_self_hosted(
    *,
    execute: bool,
    bsd_client: BSDClient | None = None,
    store: SupabaseRESTStore | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, int]:
    """Fetch one bounded window; write only when execute=True."""
    # One clock for the whole cycle. A pinned `now` pins every timestamp the cycle takes, so tests
    # and replays never depend on the wall clock (Lesson 169); production passes neither.
    if clock is None:
        pinned = now
        clock = (lambda: pinned) if pinned is not None else (lambda: datetime.now(timezone.utc))
    now = clock().astimezone(timezone.utc)
    lookahead_days = _positive_days("PRORACLE_LOOKAHEAD_DAYS", 2, 14)
    history_days = _positive_days("PRORACLE_HISTORY_DAYS", 30, 365)
    bsd = bsd_client or _environment_client()
    start_day = now.date()
    end_day = start_day + timedelta(days=lookahead_days)
    history_day = start_day - timedelta(days=history_days)

    upcoming = bsd.fetch_events("upcoming", start_day.isoformat(), end_day.isoformat())
    source_failure: BSDSourceError | None = None
    try:
        finished = bsd.fetch_events("finished", history_day.isoformat(), start_day.isoformat())
    except BSDSourceError as error:
        # Upcoming fixtures are known, so execution mode can record one safe event per fixture.
        source_failure = error
        finished = []
    retrieved_at = clock().astimezone(timezone.utc)

    invalid_upcoming = [
        match
        for match in upcoming
        if match.fixture_status != "SCHEDULED" or match.kickoff_at <= now
    ]
    prediction_candidates = [
        match
        for match in upcoming
        if match.fixture_status == "SCHEDULED" and match.kickoff_at > now
    ]
    summary = {
        "upcoming_events": len(upcoming),
        "eligible_fixtures": len(prediction_candidates),
        "historical_final_events": len(finished),
        "fixtures_synced": 0,
        "results_written": 0,
        "predictions_written": 0,
        "failures_written": 0,
        "ingest_errors": 0,
        "rating_history_results": 0,
        "rating_predictions_written": 0,
        "rating_failures": 0,
    }
    if not execute:
        return summary

    if store is None:
        project_url = os.environ.get("SUPABASE_URL", "")
        service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not project_url or not service_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_KEY are required with --execute")
        store = SupabaseRESTStore(
            project_url, service_key, schema=os.environ.get("PRORACLE_DB_SCHEMA", "proracle")
        )

    fixture_ids: dict[str, str] = {}
    for match in [*upcoming, *finished]:
        try:
            fixture_id, result_added = store.ingest_match(match, observed_at=retrieved_at)
        except SupabaseStoreError:
            # One failed write must not abandon the rest of the window. A rerun skips what is
            # already stored, so the missed event is picked up next time; the run still exits
            # non-zero below so the failure is never silent.
            summary["ingest_errors"] += 1
            continue
        fixture_ids[match.source_event_key] = str(fixture_id)
        summary["fixtures_synced"] += 1
        summary["results_written"] += int(result_added)

    if source_failure is not None:
        for match in prediction_candidates:
            if match.kickoff_at <= retrieved_at or match.source_event_key not in fixture_ids:
                continue
            event = _failure_event(
                fixture_ids[match.source_event_key],
                match.kickoff_at,
                code=source_failure.error_code,
                stage="SOURCE_REQUEST",
                retryable=source_failure.retryable,
                occurred_at=retrieved_at,
            )
            store.append_failure(event)
            summary["failures_written"] += 1
        return summary

    for match in invalid_upcoming:
        # The failure-event contract records prematch failures only.
        if match.kickoff_at <= retrieved_at or match.source_event_key not in fixture_ids:
            continue
        event = _failure_event(
            fixture_ids[match.source_event_key],
            match.kickoff_at,
            code="SOURCE_INVALID_RESPONSE",
            stage="SOURCE_REQUEST",
            retryable=True,
            occurred_at=retrieved_at,
        )
        store.append_failure(event)
        summary["failures_written"] += 1

    training = store.training_results()
    predicted_at = clock().astimezone(timezone.utc)
    # Both identities use the same prediction time, so they are compared at the same run.
    rating_state = _rating_state(store, predicted_at, summary)
    for match in prediction_candidates:
        # A slow provider/database run can outlive a fixture's prematch window.
        # Do not emit a prediction or an invalid post-kickoff failure event.
        if match.kickoff_at <= predicted_at or match.source_event_key not in fixture_ids:
            continue
        fixture_id = fixture_ids[match.source_event_key]
        _write_baseline_prediction(store, match, fixture_id, training, predicted_at, summary)
        _write_rating_prediction(store, rating_state, match, fixture_id, predicted_at, summary)
    return summary


def _write_baseline_prediction(
    store: SupabaseRESTStore,
    match: BSDMatch,
    fixture_id: str,
    training: list[Result],
    predicted_at: datetime,
    summary: dict[str, int],
) -> None:
    """The frequency-baseline identity. Its behaviour is unchanged by the rating identity."""
    if not training:
        event = _failure_event(
            fixture_id,
            match.kickoff_at,
            code="FEATURES_MISSING",
            stage="INPUT_VALIDATION",
            retryable=True,
            occurred_at=predicted_at,
        )
        store.append_failure(event)
        summary["failures_written"] += 1
        return

    try:
        feature_snapshot = validate_features(_outcome_counts(training, predicted_at))
        prediction = create_baseline_prediction(
            match_to_fixture(match, fixture_id),
            feature_snapshot,
            training,
            prediction_id=uuid4(),
            predicted_at=predicted_at,
        )
    except ValueError:
        event = _failure_event(
            fixture_id,
            match.kickoff_at,
            code="INVALID_INPUT",
            stage="INPUT_VALIDATION",
            retryable=False,
            occurred_at=predicted_at,
        )
        store.append_failure(event)
        summary["failures_written"] += 1
        return

    try:
        store.append_prediction(prediction)
    except SupabaseStoreError:
        # The persistence service may be unavailable too; do not claim that this
        # failure event was recorded if its own insert fails.
        try:
            event = _failure_event(
                fixture_id,
                match.kickoff_at,
                code="PERSISTENCE_FAILED",
                stage="PERSISTENCE",
                retryable=True,
                occurred_at=predicted_at,
            )
            store.append_failure(event)
            summary["failures_written"] += 1
        finally:
            raise
    summary["predictions_written"] += 1


def _rating_state(
    store: SupabaseRESTStore, predicted_at: datetime, summary: dict[str, int]
) -> RatingState | None:
    """Build the rating model's state once per cycle; any failure disables only that identity."""
    try:
        history = store.rating_results()
        summary["rating_history_results"] = len(history)
        return build_rating_state(history, predicted_at)
    except Exception:  # noqa: BLE001 -- isolation boundary: the baseline must never be blocked
        return None


def _write_rating_prediction(
    store: SupabaseRESTStore,
    state: RatingState | None,
    match: BSDMatch,
    fixture_id: str,
    predicted_at: datetime,
    summary: dict[str, int],
) -> None:
    """The rating identity. Never raises, so it can never block or alter the baseline.

    Its failures are counted in the run summary only. The failure-event table has no model
    identity column yet, so writing one here would be indistinguishable from a baseline failure
    for the same fixture; recording rating failures as events needs a migration (see
    docs/RATING_MODEL.md).
    """
    if state is None:
        summary["rating_failures"] += 1
        return
    try:
        prediction = create_rating_prediction(
            match_to_fixture(match, fixture_id),
            state,
            prediction_id=uuid4(),
            predicted_at=predicted_at,
        )
        store.append_prediction(prediction)
    except Exception:  # noqa: BLE001 -- isolation boundary, as above
        summary["rating_failures"] += 1
        return
    summary["rating_predictions_written"] += 1


def match_to_fixture(match: BSDMatch, fixture_id: str):
    from proracle.domain import normalize_fixture

    return normalize_fixture(match.fixture_record(fixture_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Preview or run one user-keyed BSD-to-Supabase PRORACLE cycle."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--execute",
        action="store_true",
        help="write normalized fixtures, results, predictions, and safe failures to your Supabase project",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and count the bounded source window without database writes (default)",
    )
    args = parser.parse_args(argv)
    try:
        summary = run_self_hosted(execute=args.execute)
    except BSDSourceError as error:
        print(f"PRORACLE stopped: {error.error_code}", file=sys.stderr)
        return 2
    except SupabaseStoreError as error:
        print(f"PRORACLE stopped: {error.error_code}", file=sys.stderr)
        return 3
    except ValueError as error:
        # ValueError messages contain only local config field names or safe validation labels.
        print(f"PRORACLE configuration/input error: {error}", file=sys.stderr)
        return 2
    label = "write" if args.execute else "preview"
    fields = " ".join(f"{key}={value}" for key, value in summary.items())
    if summary.get("ingest_errors"):
        # The rest of the window was processed; a rerun picks up what was missed.
        print(f"PRORACLE {label} incomplete: {fields}", file=sys.stderr)
        return 3
    print(f"PRORACLE {label} complete: {fields}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
