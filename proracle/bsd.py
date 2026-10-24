"""Allowlisted, user-keyed client for BSD football events v2."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import Message
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE_URL = "https://sports.bzzoiro.com/api/v2/events/"
# League names are fetched once per run to label fixtures: events carry a league id only.
LEAGUES_URL = "https://sports.bzzoiro.com/api/v2/leagues/"
MAX_PAGE_SIZE = 200
# A finished row with an invalid field (for example a negative score) is skipped, not repaired.
# More than max(3, 1% of the rows seen) skipped in one fetch means the provider is broken.
_INVALID_ROWS_FLOOR = 3
_INVALID_ROWS_FRACTION = 0.01
_EVENT_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_ALLOWED_SOURCE_STATUSES = frozenset(
    {
        "upcoming",
        "scheduled",
        "notstarted",
        "live",
        "finished",
        "cancelled",
        "postponed",
        "abandoned",
        "unresolved",
    }
)
# The provider's own name for a not-yet-started match is "notstarted", observed on live responses.
# The client's logical "upcoming" query maps to it; "upcoming" itself selects a different, larger set.
_PROVIDER_QUERY_STATUS = {"upcoming": "notstarted", "finished": "finished"}


class BSDSourceError(Exception):
    """A safe, stable provider failure. Never contains response or exception text."""

    def __init__(self, error_code: str, *, retryable: bool, http_status: int | None = None):
        self.error_code = error_code
        self.retryable = retryable
        self.http_status = http_status
        super().__init__(error_code)


class EventTransport(Protocol):
    def __call__(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, Mapping[str, str], bytes]: ...


def _urllib_transport(
    url: str, headers: Mapping[str, str], timeout: float
) -> tuple[int, Mapping[str, str], bytes]:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        # Read and discard the body. It can contain untrusted provider detail.
        body = error.read()
        return error.code, error.headers or Message(), body


def _utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _source_key(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("source event key is invalid")
    result = str(value)
    if not _EVENT_KEY.fullmatch(result):
        raise ValueError("source event key is invalid")
    return result


def _team_name(value: object, label: str) -> str:
    if isinstance(value, Mapping):
        value = value.get("name")
    if not isinstance(value, str):
        raise ValueError(f"{label} is missing")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 160:
        raise ValueError(f"{label} is invalid")
    return normalized


def _optional_label(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("competition reference is invalid")
    result = " ".join(str(value).split())
    if not result or len(result) > 160:
        raise ValueError("competition reference is invalid")
    return result


def _score(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} is invalid")
    return value


@dataclass(frozen=True)
class BSDMatch:
    """Minimal normalized event; it cannot hold a raw provider response."""

    source_event_key: str
    home_team: str
    away_team: str
    kickoff_at: datetime
    competition_id: str | None
    competition_name: str | None
    fixture_status: str
    home_score: int | None = None
    away_score: int | None = None

    @property
    def actual_outcome(self) -> str | None:
        if self.fixture_status != "COMPLETED":
            return None
        if self.home_score is None or self.away_score is None:
            raise ValueError("finished event has no valid regulation-time score")
        if self.home_score > self.away_score:
            return "HOME_WIN"
        if self.home_score < self.away_score:
            return "AWAY_WIN"
        return "DRAW"

    def fixture_record(self, fixture_id: str) -> dict[str, object]:
        return {
            "fixture_id": fixture_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "kickoff_at": self.kickoff_at.isoformat().replace("+00:00", "Z"),
            "competition_id": self.competition_id,
            "competition_name": self.competition_name,
        }


def normalize_bsd_event(value: object) -> BSDMatch:
    """Copy the approved fixture/result fields and discard everything else."""
    if not isinstance(value, Mapping):
        raise ValueError("event must be an object")
    if "id" in value and "event_id" in value and value["id"] != value["event_id"]:
        raise ValueError("event identifiers conflict")
    key = _source_key(value.get("id", value.get("event_id")))
    home = _team_name(value.get("home_team", value.get("home_team_name")), "home team")
    away = _team_name(value.get("away_team", value.get("away_team_name")), "away team")
    if home.casefold() == away.casefold():
        raise ValueError("home and away teams must differ")

    kickoff_value = value.get("event_date", value.get("kickoff_at", value.get("kickoff")))
    kickoff_at = _utc_timestamp(kickoff_value, "kickoff")
    raw_status = value.get("status")
    if not isinstance(raw_status, str) or raw_status not in _ALLOWED_SOURCE_STATUSES:
        raise ValueError("event status is missing or unsupported")
    fixture_status = {
        "upcoming": "SCHEDULED",
        "scheduled": "SCHEDULED",
        "notstarted": "SCHEDULED",
        "live": "LIVE",
        "finished": "COMPLETED",
        "cancelled": "CANCELLED",
        "postponed": "POSTPONED",
        "abandoned": "ABANDONED",
        "unresolved": "UNKNOWN",
    }[raw_status]

    league = value.get("league")
    if league is not None and not isinstance(league, Mapping):
        raise ValueError("competition data is invalid")
    competition_id = _optional_label(
        value.get("league_id", league.get("id") if isinstance(league, Mapping) else None)
    )
    competition_name = _optional_label(
        value.get(
            "competition_name",
            value.get("league_name", league.get("name") if isinstance(league, Mapping) else None),
        )
    )

    home_score = away_score = None
    if fixture_status == "COMPLETED":
        home_score = _score(value.get("home_score"), "home score")
        away_score = _score(value.get("away_score"), "away score")

    return BSDMatch(
        source_event_key=key,
        home_team=home,
        away_team=away,
        kickoff_at=kickoff_at,
        competition_id=competition_id,
        competition_name=competition_name,
        fixture_status=fixture_status,
        home_score=home_score,
        away_score=away_score,
    )


class BSDClient:
    """Small v2 events client with bounded pagination and safe retry behavior."""

    def __init__(
        self,
        token: str,
        *,
        transport: EventTransport = _urllib_transport,
        sleeper: Callable[[float], None] = time.sleep,
        timeout_seconds: float = 10.0,
        page_size: int = MAX_PAGE_SIZE,
        max_pages: int = 50,
        max_retries: int = 1,
        max_retry_after_seconds: float = 10.0,
    ) -> None:
        if not isinstance(token, str) or not token.strip() or "\n" in token or "\r" in token:
            raise ValueError("BSD_API_TOKEN must be set to a single-line token")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError("page_size must be between 1 and 200")
        if not 1 <= max_pages <= 500 or not 0 <= max_retries <= 2:
            raise ValueError("pagination/retry bounds are invalid")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not math.isfinite(max_retry_after_seconds) or max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds must be finite and non-negative")
        self._token = token.strip()
        self._transport = transport
        self._sleep = sleeper
        self._timeout = timeout_seconds
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_retries = max_retries
        self._max_retry_after = max_retry_after_seconds
        # Finished rows skipped as invalid by the most recent fetch_events call (a count only).
        self.invalid_rows_skipped = 0
        # League rows skipped as invalid by the most recent fetch_league_names call (a count only).
        self.league_rows_skipped = 0

    @staticmethod
    def _date(value: str, label: str) -> str:
        try:
            return date.fromisoformat(value).isoformat()
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label} must be YYYY-MM-DD") from error

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        for key, value in headers.items():
            if key.casefold() == name.casefold():
                return value
        return None

    @staticmethod
    def _provider_code(body: bytes) -> str | None:
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, Mapping):
            return None
        code = value.get("code")
        return code if isinstance(code, str) and len(code) <= 64 else None

    def _get_page(
        self, params: Mapping[str, str], base_url: str = API_BASE_URL
    ) -> Mapping[str, Any]:
        url = f"{base_url}?{urlencode(params)}"
        headers = {"Authorization": f"Token {self._token}", "Accept": "application/json"}
        for attempt in range(self._max_retries + 1):
            try:
                status, response_headers, body = self._transport(url, headers, self._timeout)
            except TimeoutError as error:
                if attempt < self._max_retries:
                    self._sleep(min(0.25 * (2**attempt), self._max_retry_after))
                    continue
                raise BSDSourceError("SOURCE_TIMEOUT", retryable=True) from error
            except (URLError, OSError) as error:
                reason = getattr(error, "reason", None)
                if isinstance(reason, TimeoutError):
                    code = "SOURCE_TIMEOUT"
                else:
                    code = "SOURCE_UNAVAILABLE"
                if attempt < self._max_retries:
                    self._sleep(min(0.25 * (2**attempt), self._max_retry_after))
                    continue
                raise BSDSourceError(code, retryable=True) from error

            if status == 401 or status == 403:
                raise BSDSourceError("SOURCE_UNAUTHORIZED", retryable=False, http_status=status)
            if status == 429:
                code = self._provider_code(body)
                if code == "taster_exhausted":
                    raise BSDSourceError("SOURCE_QUOTA_EXHAUSTED", retryable=True, http_status=429)
                if code != "rate_limited":
                    raise BSDSourceError("SOURCE_RATE_LIMITED", retryable=True, http_status=429)
                retry_after = self._header(response_headers, "Retry-After")
                try:
                    wait_seconds = float(retry_after) if retry_after is not None else 1.0
                except ValueError:
                    wait_seconds = self._max_retry_after + 1
                if (
                    attempt < self._max_retries
                    and math.isfinite(wait_seconds)
                    and 0 <= wait_seconds <= self._max_retry_after
                ):
                    self._sleep(wait_seconds)
                    continue
                raise BSDSourceError("SOURCE_RATE_LIMITED", retryable=True, http_status=429)
            if status >= 500:
                if attempt < self._max_retries:
                    self._sleep(min(0.25 * (2**attempt), self._max_retry_after))
                    continue
                raise BSDSourceError("SOURCE_UNAVAILABLE", retryable=True, http_status=status)
            if status < 200 or status >= 300:
                raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False, http_status=status)
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False, http_status=status) from error
            if not isinstance(payload, Mapping) or not isinstance(payload.get("results"), list):
                raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False, http_status=status)
            return payload
        raise BSDSourceError("SOURCE_UNAVAILABLE", retryable=True)

    def fetch_events(self, status: str, date_from: str, date_to: str) -> list[BSDMatch]:
        if status not in {"upcoming", "finished"}:
            raise ValueError("only upcoming and finished source queries are supported")
        start = self._date(date_from, "date_from")
        end = self._date(date_to, "date_to")
        if start > end:
            raise ValueError("date_from must not follow date_to")

        self.invalid_rows_skipped = 0
        rows_seen = 0
        normalized: list[BSDMatch] = []
        seen_keys: set[str] = set()
        offset = 0
        expected_count: int | None = None
        for _page in range(self._max_pages):
            payload = self._get_page(
                {
                    "status": _PROVIDER_QUERY_STATUS[status],
                    "date_from": start,
                    "date_to": end,
                    "limit": str(self._page_size),
                    "offset": str(offset),
                }
            )
            rows = payload["results"]
            count = payload.get("count")
            if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
                raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
            if expected_count is None and count is not None:
                expected_count = count
            elif count is not None and expected_count != count:
                raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
            if not rows:
                if expected_count is not None and offset < expected_count:
                    raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
                break
            if len(rows) > self._page_size:
                raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
            for row in rows:
                rows_seen += 1
                try:
                    match = normalize_bsd_event(row)
                except (TypeError, ValueError) as error:
                    # Fail closed on the row, not the window, for finished events only. Never
                    # repair or default a value; a non-object row is still a whole-response failure.
                    if status != "finished" or not isinstance(row, Mapping):
                        raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False) from error
                    self.invalid_rows_skipped += 1
                    continue
                if match.source_event_key in seen_keys:
                    raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
                seen_keys.add(match.source_event_key)
                normalized.append(match)
            offset += len(rows)
            if expected_count is not None and offset >= expected_count:
                break
            if len(rows) < self._page_size:
                if expected_count is not None and offset < expected_count:
                    raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
                break
        else:
            if expected_count is not None and offset < expected_count:
                raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
        if self.invalid_rows_skipped > max(_INVALID_ROWS_FLOOR, int(rows_seen * _INVALID_ROWS_FRACTION)):
            raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
        return normalized

    def fetch_league_names(self) -> dict[str, str]:
        """League id -> display name, so fixtures can carry a competition name.

        Names are labels only. A row without a usable id or name is skipped and counted; a malformed
        response, an inconsistent count or a truncated read fails closed with a stable code.
        """
        self.league_rows_skipped = 0
        names: dict[str, str] = {}
        offset = 0
        expected_count: int | None = None
        for _page in range(self._max_pages):
            payload = self._get_page(
                {"limit": str(self._page_size), "offset": str(offset)}, base_url=LEAGUES_URL
            )
            rows = payload["results"]
            count = payload.get("count")
            if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
                raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
            if expected_count is None and count is not None:
                expected_count = count
            elif count is not None and expected_count != count:
                raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
            if not rows:
                if expected_count is not None and offset < expected_count:
                    raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
                break
            if len(rows) > self._page_size:
                raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
            for row in rows:
                if not isinstance(row, Mapping):
                    raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
                try:
                    league_id = _optional_label(row.get("id"))
                    name = _optional_label(row.get("name"))
                except ValueError:
                    league_id, name = None, None
                if league_id is None or name is None:
                    self.league_rows_skipped += 1
                    continue
                names[league_id] = name
            offset += len(rows)
            if expected_count is not None and offset >= expected_count:
                break
            if len(rows) < self._page_size:
                if expected_count is not None and offset < expected_count:
                    raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
                break
        else:
            if expected_count is not None and offset < expected_count:
                raise BSDSourceError("SOURCE_INVALID_RESPONSE", retryable=False)
        return names
