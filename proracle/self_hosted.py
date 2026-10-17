"""Run one operator's local BSD-to-Supabase prediction cycle."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from proracle.bsd import BSDClient, BSDMatch, BSDSourceError
from proracle.contracts import PredictionFailureEvent
from proracle.domain import create_baseline_prediction, validate_features
from proracle.evaluate import Result
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
    return BSDClient(token)


def run_self_hosted(
    *,
    execute: bool,
    bsd_client: BSDClient | None = None,
    store: SupabaseRESTStore | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Fetch one bounded window; write only when execute=True."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
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
    retrieved_at = datetime.now(timezone.utc)

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
        fixture_id, result_added = store.ingest_match(match, observed_at=retrieved_at)
        fixture_ids[match.source_event_key] = str(fixture_id)
        summary["fixtures_synced"] += 1
        summary["results_written"] += int(result_added)

    if source_failure is not None:
        for match in prediction_candidates:
            if match.kickoff_at <= retrieved_at:
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
        if match.kickoff_at <= retrieved_at:
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
    predicted_at = datetime.now(timezone.utc)
    for match in prediction_candidates:
        # A slow provider/database run can outlive a fixture's prematch window.
        # Do not emit a prediction or an invalid post-kickoff failure event.
        if match.kickoff_at <= predicted_at:
            continue
        fixture_id = fixture_ids[match.source_event_key]
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
            continue

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
            continue

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
    return summary


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
    print(f"PRORACLE {label} complete: {fields}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
