"""Minimal server-side Supabase REST adapter for one operator's installation."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from proracle.bsd import BSDMatch
from proracle.contracts import PredictionFailureEvent, PredictionRecord, parse_prediction_record
from proracle.evaluate import Result
from proracle.rating import RatingResult
from proracle.retrospective import (
    ResultVersion,
    StoredPrediction,
    current_training_results,
)


class SupabaseStoreError(Exception):
    """Safe database failure with no URL, key, response body, or raw exception text."""

    def __init__(self, *, http_status: int | None = None):
        self.error_code = "PERSISTENCE_FAILED"
        self.retryable = http_status is None or http_status >= 500
        self.http_status = http_status
        super().__init__(self.error_code)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise SupabaseStoreError()
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise SupabaseStoreError() from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SupabaseStoreError()
    return parsed.astimezone(timezone.utc)


class SupabaseRESTStore:
    """Use only the server-side Data API; the service key must remain private."""

    def __init__(
        self,
        project_url: str,
        service_key: str,
        *,
        schema: str = "proracle",
        timeout_seconds: float = 10.0,
        page_size: int = 500,
        max_pages: int = 100,
        max_retries: int = 3,
        api_key: str | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlsplit(project_url.strip() if isinstance(project_url, str) else "")
        local_host = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme not in ({"https"} if not local_host else {"https", "http"})
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("SUPABASE_URL must be a project base URL without path or credentials")
        if not isinstance(service_key, str) or not service_key.strip() or "\n" in service_key:
            raise ValueError("SUPABASE_SERVICE_KEY must be set to a single-line server key")
        if not isinstance(schema, str) or re.fullmatch(r"[a-z_][a-z0-9_]*", schema) is None:
            raise ValueError("PRORACLE_DB_SCHEMA must be a lowercase PostgreSQL schema name")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 500:
            raise ValueError("database pagination bounds are invalid")
        if not 0 <= max_retries <= 5:
            raise ValueError("max_retries must be between 0 and 5")
        self._base_url = urlunsplit(
            (parsed.scheme, parsed.netloc, "", "", "")
        ).rstrip("/")
        if api_key is not None and (
            not isinstance(api_key, str) or not api_key.strip() or "\n" in api_key
        ):
            raise ValueError("SUPABASE_API_KEY must be a single-line project key")
        self._service_key = service_key.strip()
        # The gateway wants a project key in `apikey` (the public publishable key is enough) and
        # the credential that selects the database role in `Authorization`. With the restricted
        # runtime role, the bearer is that role's token and `apikey` is the publishable key.
        self._api_key = api_key.strip() if api_key else self._service_key
        self._schema = schema
        self._timeout = timeout_seconds
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_retries = max_retries
        self._sleep = sleeper

    # Only requests that are safe to repeat are retried. GET reads; PATCH sets the same
    # values again. POST is never retried: a timed-out insert may have succeeded, and a
    # repeat could append a duplicate row. A failed POST is left to the caller and to the
    # next scheduled run, which skips what is already stored.
    _RETRYABLE_METHODS = frozenset({"GET", "PATCH"})

    def _request(self, method: str, table: str, **kwargs: Any) -> tuple[Any, Mapping[str, str]]:
        attempt = 0
        while True:
            try:
                return self._request_once(method, table, **kwargs)
            except SupabaseStoreError as error:
                transient = error.http_status is None or error.http_status >= 500 or error.http_status == 429
                if method not in self._RETRYABLE_METHODS or not transient or attempt >= self._max_retries:
                    raise
                self._sleep(min(0.5 * (2**attempt), 8.0))
                attempt += 1

    def _request_once(
        self,
        method: str,
        table: str,
        *,
        query: Sequence[tuple[str, str]] = (),
        payload: Mapping[str, object] | None = None,
        prefer: str | None = None,
        item_range: tuple[int, int] | None = None,
    ) -> tuple[Any, Mapping[str, str]]:
        if table not in {"fixtures", "predictions", "results", "prediction_failures"}:
            raise ValueError("unsupported PRORACLE table")
        query_string = urlencode(query)
        url = f"{self._base_url}/rest/v1/{table}"
        if query_string:
            url += f"?{query_string}"
        headers = {
            "apikey": self._api_key,
            "Authorization": f"Bearer {self._service_key}",
            "Accept": "application/json",
            "Accept-Profile": self._schema,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Profile"] = self._schema
        if prefer:
            headers["Prefer"] = prefer
        if item_range is not None:
            headers["Range-Unit"] = "items"
            headers["Range"] = f"{item_range[0]}-{item_range[1]}"
        request = Request(
            url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                body = response.read()
                status = response.status
                response_headers = response.headers
        except HTTPError as error:
            # The body and URL are deliberately discarded; neither enters logs or exceptions.
            error.read()
            raise SupabaseStoreError(http_status=error.code) from None
        except (URLError, OSError, TimeoutError):
            raise SupabaseStoreError() from None
        if status < 200 or status >= 300:
            raise SupabaseStoreError(http_status=status)
        if not body:
            return None, response_headers
        try:
            result = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SupabaseStoreError(http_status=status) from None
        return result, response_headers

    @staticmethod
    def _rows(value: object) -> list[Mapping[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
            raise SupabaseStoreError()
        return value

    def _save_fixture(self, match: BSDMatch) -> UUID:
        existing_value, _ = self._request(
            "GET",
            "fixtures",
            query=(
                ("select", "id,kickoff_at,status,source_event_key"),
                ("source_name", "eq.bsd"),
                ("source_event_key", f"eq.{match.source_event_key}"),
            ),
        )
        existing = self._rows(existing_value)
        if len(existing) > 1:
            raise SupabaseStoreError()
        if existing:
            try:
                fixture_id = UUID(str(existing[0]["id"]))
            except (KeyError, ValueError, AttributeError) as error:
                raise SupabaseStoreError() from error
            self._request(
                "PATCH",
                "fixtures",
                query=(("id", f"eq.{fixture_id}"),),
                payload={"kickoff_at": _utc_text(match.kickoff_at), "status": match.fixture_status},
                prefer="return=minimal",
            )
            return fixture_id

        fixture_id = uuid4()
        payload = {
            "id": str(fixture_id),
            "sport": "football",
            "competition_id": match.competition_id,
            "competition_name": match.competition_name,
            "home_team": match.home_team,
            "away_team": match.away_team,
            "kickoff_at": _utc_text(match.kickoff_at),
            "status": match.fixture_status,
            "source_name": "bsd",
            "source_event_key": match.source_event_key,
        }
        try:
            self._request("POST", "fixtures", payload=payload, prefer="return=minimal")
            return fixture_id
        except SupabaseStoreError as error:
            # A competing run may win the source-key unique index between our read and insert.
            if error.http_status != 409:
                raise
            retry_value, _ = self._request(
                "GET",
                "fixtures",
                query=(
                    ("select", "id"),
                    ("source_name", "eq.bsd"),
                    ("source_event_key", f"eq.{match.source_event_key}"),
                ),
            )
            retry_rows = self._rows(retry_value)
            if len(retry_rows) != 1:
                raise
            return UUID(str(retry_rows[0]["id"]))

    def ingest_match(self, match: BSDMatch, observed_at: datetime) -> tuple[UUID, bool]:
        fixture_id = self._save_fixture(match)
        outcome = match.actual_outcome
        if outcome is None:
            return fixture_id, False
        if observed_at.astimezone(timezone.utc) < match.kickoff_at:
            raise ValueError("a finished result cannot be available before kickoff")

        existing_value, _ = self._request(
            "GET",
            "results",
            query=(
                ("select", "id,supersedes_result_id,status,actual_outcome"),
                ("fixture_id", f"eq.{fixture_id}"),
            ),
        )
        versions = self._rows(existing_value)
        superseded = {
            str(row["supersedes_result_id"])
            for row in versions
            if row.get("supersedes_result_id") is not None
        }
        current = [row for row in versions if str(row.get("id")) not in superseded]
        if len(current) > 1:
            raise SupabaseStoreError()
        if current and current[0].get("status") == "FINAL" and current[0].get("actual_outcome") == outcome:
            return fixture_id, False

        payload = {
            "id": str(uuid4()),
            "fixture_id": str(fixture_id),
            "supersedes_result_id": str(current[0]["id"]) if current else None,
            "status": "FINAL",
            "actual_outcome": outcome,
            "observed_at": _utc_text(observed_at),
            "finalized_at": _utc_text(observed_at),
            "source_name": "bsd",
            "source_retrieved_at": _utc_text(observed_at),
        }
        self._request("POST", "results", payload=payload, prefer="return=minimal")
        return fixture_id, True

    @staticmethod
    def _content_range_total(headers: Mapping[str, str]) -> int:
        """The exact row total from `Content-Range: 0-499/1234`. Missing or unknown fails closed."""
        value = None
        for key in headers.keys():
            if key.casefold() == "content-range":
                value = headers[key]
                break
        if not isinstance(value, str) or "/" not in value:
            raise SupabaseStoreError()
        total = value.rsplit("/", 1)[1].strip()
        if not total.isdigit():
            raise SupabaseStoreError()
        return int(total)

    def _fetch_all(self, table: str, query: Sequence[tuple[str, str]]) -> list[Mapping[str, Any]]:
        """Read every row, and prove it: the rows collected must equal the database's own total.

        A page cap, a short page, or a table that changed mid-read must never pass as a complete read
        (Lesson 163: a silently truncated read looked like a small, valid dataset).
        """
        rows: list[Mapping[str, Any]] = []
        total: int | None = None
        for page in range(self._max_pages):
            start = page * self._page_size
            value, headers = self._request(
                "GET",
                table,
                query=query,
                prefer="count=exact",
                item_range=(start, start + self._page_size - 1),
            )
            page_total = self._content_range_total(headers)
            if total is None:
                total = page_total
            elif page_total != total:
                raise SupabaseStoreError()
            batch = self._rows(value)
            rows.extend(batch)
            if len(rows) >= total or len(batch) < self._page_size:
                if len(rows) != total:
                    raise SupabaseStoreError()
                return rows
        raise SupabaseStoreError()

    _RESULT_VERSION_COLUMNS = (
        "id,fixture_id,supersedes_result_id,status,actual_outcome,observed_at,source_retrieved_at"
    )

    @staticmethod
    def _result_version(row: Mapping[str, Any]) -> ResultVersion:
        fixture = row.get("fixture")
        fixture_id = row.get("fixture_id")
        outcome = row.get("actual_outcome")
        supersedes = row.get("supersedes_result_id")
        if (
            not isinstance(fixture, Mapping)
            or not isinstance(fixture_id, str)
            or not isinstance(row.get("id"), str)
            or not isinstance(row.get("status"), str)
            or (outcome is not None and outcome not in {"HOME_WIN", "DRAW", "AWAY_WIN"})
            or (supersedes is not None and not isinstance(supersedes, str))
        ):
            raise SupabaseStoreError()
        kickoff_at = _parse_time(fixture.get("kickoff_at"), "kickoff_at")
        available_at = _parse_time(
            row.get("source_retrieved_at") or row.get("observed_at"), "available_at"
        )
        if available_at < kickoff_at:
            raise SupabaseStoreError()
        return ResultVersion(
            result_id=row["id"],
            fixture_id=fixture_id,
            supersedes_id=supersedes,
            status=row["status"],
            actual_outcome=outcome,
            kickoff_at=kickoff_at,
            available_at=available_at,
        )

    def result_versions(self) -> list[ResultVersion]:
        """Read every result version, whatever its status, so supersession stays intact."""
        rows = self._fetch_all(
            "results",
            (
                (
                    "select",
                    f"{self._RESULT_VERSION_COLUMNS},fixture:fixtures!results_fixture_id_fkey(kickoff_at)",
                ),
                ("order", "id.asc"),
            ),
        )
        return [self._result_version(row) for row in rows]

    def rating_results(self) -> list[RatingResult]:
        """Current FINAL results with their fixture's team labels, for the rating model.

        Same exact-total paginated read and the same correction resolution as
        ``training_results``; the fixture join adds only the allowlisted team labels.
        """
        rows = self._fetch_all(
            "results",
            (
                (
                    "select",
                    f"{self._RESULT_VERSION_COLUMNS},fixture:fixtures!results_fixture_id_fkey(kickoff_at,home_team,away_team)",
                ),
                ("order", "id.asc"),
            ),
        )
        versions: list[ResultVersion] = []
        teams: dict[str, tuple[str, str]] = {}
        for row in rows:
            version = self._result_version(row)
            fixture = row["fixture"]
            home, away = fixture.get("home_team"), fixture.get("away_team")
            if not isinstance(home, str) or not isinstance(away, str):
                raise SupabaseStoreError()
            if teams.setdefault(version.fixture_id, (home, away)) != (home, away):
                raise SupabaseStoreError()
            versions.append(version)
        try:
            current, _non_final = current_training_results(versions)
        except ValueError:
            raise SupabaseStoreError() from None
        return [
            RatingResult(
                fixture_id=item.fixture_id,
                home_team=teams[item.fixture_id][0],
                away_team=teams[item.fixture_id][1],
                kickoff_at=item.kickoff_at,
                available_at=item.available_at,  # type: ignore[arg-type]
                actual_outcome=item.actual_outcome,
            )
            for item in current
        ]

    def training_results(self) -> list[Result]:
        # Resolve corrections across all statuses first: a later VOID/CANCELLED version
        # must remove the earlier FINAL label rather than leave it current.
        training, _non_final = current_training_results(self.result_versions())
        return training

    def stored_predictions(self) -> list[StoredPrediction]:
        """Read immutable prediction rows with their fixture's competition label."""
        rows = self._fetch_all(
            "predictions",
            (
                (
                    "select",
                    "id,fixture_id,contract_version,predicted_at,kickoff_at,model_provider,model_name,model_version,prompt_version,input_schema_version,input_snapshot_hash,home_win_probability,draw_probability,away_win_probability,fixture:fixtures!predictions_fixture_id_fkey(competition_id)",
                ),
                ("order", "id.asc"),
            ),
        )
        stored: list[StoredPrediction] = []
        for row in rows:
            fixture = row.get("fixture")
            if not isinstance(fixture, Mapping):
                raise SupabaseStoreError()
            try:
                record = parse_prediction_record(
                    {
                        "contract_version": row["contract_version"],
                        "prediction_id": row["id"],
                        "fixture_id": row["fixture_id"],
                        "predicted_at": row["predicted_at"],
                        "kickoff_at": row["kickoff_at"],
                        "model": {
                            "provider": row["model_provider"],
                            "name": row["model_name"],
                            "version": row["model_version"],
                        },
                        "prompt_version": row["prompt_version"],
                        "input_schema_version": row["input_schema_version"],
                        "input_snapshot_hash": row["input_snapshot_hash"],
                        "probabilities": {
                            "HOME_WIN": float(row["home_win_probability"]),
                            "DRAW": float(row["draw_probability"]),
                            "AWAY_WIN": float(row["away_win_probability"]),
                        },
                    }
                )
            except (KeyError, TypeError, ValueError):
                raise SupabaseStoreError() from None
            competition = fixture.get("competition_id")
            stored.append(
                StoredPrediction(
                    record=record, competition_id=competition if isinstance(competition, str) else None
                )
            )
        return stored

    def append_prediction(self, record: PredictionRecord) -> None:
        self._request(
            "POST",
            "predictions",
            payload={
                "id": str(record.prediction_id),
                "fixture_id": str(record.fixture_id),
                "contract_version": record.contract_version,
                "predicted_at": _utc_text(record.predicted_at),
                "kickoff_at": _utc_text(record.kickoff_at),
                "model_provider": record.model.provider,
                "model_name": record.model.name,
                "model_version": record.model.version,
                "prompt_version": record.prompt_version,
                "input_schema_version": record.input_schema_version,
                "input_snapshot_hash": record.input_snapshot_hash,
                "home_win_probability": record.probabilities.home_win,
                "draw_probability": record.probabilities.draw,
                "away_win_probability": record.probabilities.away_win,
            },
            prefer="return=minimal",
        )

    def append_failure(self, event: PredictionFailureEvent) -> None:
        self._request(
            "POST",
            "prediction_failures",
            payload=event.as_dict(),
            prefer="return=minimal",
        )
