"""Offline tests for safe Supabase REST mapping and result availability."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from uuid import UUID
from unittest.mock import patch

from proracle.bsd import BSDMatch
from proracle.evaluate import Result
from proracle.supabase_adapter import SupabaseRESTStore, SupabaseStoreError


class Response:
    status = 201
    headers = {"Content-Type": "application/json"}

    def __init__(self, body: bytes = b""):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


def match() -> BSDMatch:
    return BSDMatch(
        source_event_key="local-123",
        home_team="Synthetic Home",
        away_team="Synthetic Away",
        kickoff_at=datetime(2030, 1, 1, 18, tzinfo=timezone.utc),
        competition_id="league-1",
        competition_name="Synthetic League",
        fixture_status="SCHEDULED",
    )


class SupabaseAdapterTests(unittest.TestCase):
    def test_request_keeps_key_in_headers_and_posts_only_approved_record_fields(self) -> None:
        store = SupabaseRESTStore("https://project.example", "test-service-secret")
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        with patch("proracle.supabase_adapter.urlopen", fake_urlopen):
            store._request(
                "POST",
                "prediction_failures",
                payload={
                    "contract_version": "1.0",
                    "event_type": "prediction_failed",
                    "error_code": "SOURCE_TIMEOUT",
                },
                prefer="return=minimal",
            )
        request = captured["request"]
        self.assertNotIn("test-service-secret", request.full_url)
        self.assertEqual(request.get_header("Apikey"), "test-service-secret")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-service-secret")
        self.assertEqual(request.get_header("Content-profile"), "proracle")
        self.assertEqual(json.loads(request.data), {
            "contract_version": "1.0",
            "event_type": "prediction_failed",
            "error_code": "SOURCE_TIMEOUT",
        })
        self.assertEqual(captured["timeout"], 10.0)

    def test_get_requests_select_the_configured_schema(self) -> None:
        store = SupabaseRESTStore(
            "https://project.example", "test-service-secret", schema="research_data"
        )
        captured = {}

        def fake_urlopen(request, timeout):
            captured["request"] = request
            return Response(b"[]")

        with patch("proracle.supabase_adapter.urlopen", fake_urlopen):
            store._request("GET", "fixtures")
        self.assertEqual(captured["request"].get_header("Accept-profile"), "research_data")

    def test_rejects_unsafe_schema_names(self) -> None:
        for schema in ("public;drop schema public", "MixedCase", "schema.name", ""):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                SupabaseRESTStore("https://project.example", "test-service-secret", schema=schema)

    def test_http_error_discards_response_body_and_secret(self) -> None:
        store = SupabaseRESTStore("https://project.example", "test-service-secret")
        import io
        from urllib.error import HTTPError

        error = HTTPError(
            "https://project.example/rest/v1/fixtures",
            403,
            "forbidden",
            {},
            io.BytesIO(b"PRIVATE_PROVIDER_DETAIL test-service-secret"),
        )
        with patch("proracle.supabase_adapter.urlopen", side_effect=error):
            with self.assertRaises(SupabaseStoreError) as caught:
                store._request("GET", "fixtures")
        self.assertEqual(str(caught.exception), "PERSISTENCE_FAILED")
        self.assertNotIn("PRIVATE_PROVIDER_DETAIL", str(caught.exception))
        self.assertNotIn("test-service-secret", str(caught.exception))

    def test_training_filters_superseded_rows_and_uses_retrieval_availability(self) -> None:
        store = SupabaseRESTStore("https://project.example", "test-service-secret")
        rows = [
            {
                "id": "r1",
                "fixture_id": "f1",
                "supersedes_result_id": None,
                "status": "FINAL",
                "actual_outcome": "HOME_WIN",
                "observed_at": "2025-01-01T20:00:00Z",
                "source_retrieved_at": "2025-01-01T20:05:00Z",
                "fixture": {"kickoff_at": "2025-01-01T18:00:00Z"},
            },
            {
                "id": "r2",
                "fixture_id": "f1",
                "supersedes_result_id": "r1",
                "status": "FINAL",
                "actual_outcome": "DRAW",
                "observed_at": "2025-01-02T20:00:00Z",
                "source_retrieved_at": "2025-01-02T20:05:00Z",
                "fixture": {"kickoff_at": "2025-01-01T18:00:00Z"},
            },
        ]
        store._request = lambda *_args, **_kwargs: (rows, {"Content-Range": f"0-{max(len(rows) - 1, 0)}/{len(rows)}"})  # type: ignore[method-assign]
        result = store.training_results()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].actual_outcome, "DRAW")
        self.assertEqual(result[0].available_at, datetime(2025, 1, 2, 20, 5, tzinfo=timezone.utc))

    def test_corrected_final_result_appends_version_that_supersedes_current_record(self) -> None:
        store = SupabaseRESTStore("https://project.example", "test-service-secret")
        fixture_id = UUID("00000000-0000-4000-8000-000000000201")
        store._save_fixture = lambda _match: fixture_id  # type: ignore[method-assign]
        captured = []

        def request(method, table, **kwargs):
            if method == "GET" and table == "results":
                return ([{
                    "id": "00000000-0000-4000-8000-000000000202",
                    "supersedes_result_id": None,
                    "status": "FINAL",
                    "actual_outcome": "HOME_WIN",
                }], {})
            captured.append((method, table, kwargs))
            return None, {}

        store._request = request  # type: ignore[method-assign]
        correction = BSDMatch(
            source_event_key="local-123",
            home_team="Synthetic Home",
            away_team="Synthetic Away",
            kickoff_at=datetime(2029, 12, 31, 18, tzinfo=timezone.utc),
            competition_id="league-1",
            competition_name="Synthetic League",
            fixture_status="COMPLETED",
            home_score=0,
            away_score=1,
        )
        inserted_fixture, added = store.ingest_match(
            correction, observed_at=datetime(2030, 1, 1, 20, tzinfo=timezone.utc)
        )
        self.assertEqual(inserted_fixture, fixture_id)
        self.assertTrue(added)
        self.assertEqual(len(captured), 1)
        payload = captured[0][2]["payload"]
        self.assertEqual(payload["actual_outcome"], "AWAY_WIN")
        self.assertEqual(
            payload["supersedes_result_id"],
            "00000000-0000-4000-8000-000000000202",
        )

    def test_rejects_remote_http_and_urls_with_embedded_credentials(self) -> None:
        with self.assertRaises(ValueError):
            SupabaseRESTStore("http://project.example", "test-service-secret")
        with self.assertRaises(ValueError):
            SupabaseRESTStore("https://user:password@project.example", "test-service-secret")


class PagedResponse(Response):
    def __init__(self, rows: list, content_range: str | None):
        super().__init__(json.dumps(rows).encode())
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        if content_range is not None:
            self.headers["Content-Range"] = content_range


class PaginationTotalTests(unittest.TestCase):
    def _store(self, page_size: int = 2, max_pages: int = 100) -> SupabaseRESTStore:
        return SupabaseRESTStore(
            "https://project.example",
            "test-service-secret",
            page_size=page_size,
            max_pages=max_pages,
            max_retries=0,
        )

    def _run(self, store: SupabaseRESTStore, pages: list[PagedResponse]):
        calls = iter(pages)
        with patch("proracle.supabase_adapter.urlopen", lambda request, timeout: next(calls)):
            return store._fetch_all("results", ())

    def test_complete_read_matches_the_database_total(self) -> None:
        rows = self._run(
            self._store(),
            [PagedResponse([{"i": 1}, {"i": 2}], "0-1/3"), PagedResponse([{"i": 3}], "2-2/3")],
        )
        self.assertEqual([r["i"] for r in rows], [1, 2, 3])

    def test_exact_multiple_of_page_size_does_not_request_a_page_past_the_end(self) -> None:
        rows = self._run(self._store(), [PagedResponse([{"i": 1}, {"i": 2}], "0-1/2")])
        self.assertEqual(len(rows), 2)

    def test_page_cap_that_truncates_the_read_fails_closed(self) -> None:
        store = self._store(page_size=1, max_pages=2)
        with self.assertRaises(SupabaseStoreError):
            self._run(store, [PagedResponse([{"i": 1}], "0-0/5"), PagedResponse([{"i": 2}], "1-1/5")])

    def test_short_read_total_mismatch_fails_closed(self) -> None:
        with self.assertRaises(SupabaseStoreError):
            self._run(self._store(), [PagedResponse([{"i": 1}], "0-0/4")])

    def test_missing_or_unknown_total_fails_closed(self) -> None:
        for header in (None, "0-1/*", "garbage"):
            with self.assertRaises(SupabaseStoreError):
                self._run(self._store(), [PagedResponse([{"i": 1}, {"i": 2}], header)])

    def test_total_changing_between_pages_fails_closed(self) -> None:
        with self.assertRaises(SupabaseStoreError):
            self._run(
                self._store(),
                [PagedResponse([{"i": 1}, {"i": 2}], "0-1/3"), PagedResponse([{"i": 3}], "2-2/4")],
            )


class RetryTests(unittest.TestCase):
    def _store(self, sleeps: list[float], retries: int = 3) -> SupabaseRESTStore:
        return SupabaseRESTStore(
            "https://project.example",
            "test-service-secret",
            max_retries=retries,
            sleeper=sleeps.append,
        )

    def test_get_is_retried_after_transport_errors_then_succeeds(self) -> None:
        sleeps: list[float] = []
        calls = []

        def flaky(request, timeout):
            calls.append(request.get_method())
            if len(calls) < 3:
                raise TimeoutError()
            return Response(b"[]")

        with patch("proracle.supabase_adapter.urlopen", flaky):
            value, _ = self._store(sleeps)._request("GET", "fixtures")
        self.assertEqual(value, [])
        self.assertEqual(calls, ["GET", "GET", "GET"])
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_patch_is_retried_on_server_error(self) -> None:
        import io
        from urllib.error import HTTPError

        sleeps: list[float] = []
        calls = []

        def flaky(request, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPError("https://project.example/x", 503, "busy", {}, io.BytesIO(b"detail"))
            return Response()

        with patch("proracle.supabase_adapter.urlopen", flaky):
            self._store(sleeps)._request("PATCH", "fixtures", payload={"status": "SCHEDULED"})
        self.assertEqual(len(calls), 2)

    def test_post_is_never_retried(self) -> None:
        sleeps: list[float] = []
        calls = []

        def failing(request, timeout):
            calls.append(1)
            raise TimeoutError()

        with patch("proracle.supabase_adapter.urlopen", failing):
            with self.assertRaises(SupabaseStoreError):
                self._store(sleeps)._request("POST", "results", payload={"id": "x"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_client_errors_are_not_retried_and_retries_are_bounded(self) -> None:
        import io
        from urllib.error import HTTPError

        sleeps: list[float] = []
        calls = []

        def forbidden(request, timeout):
            calls.append(1)
            raise HTTPError("https://project.example/x", 403, "no", {}, io.BytesIO(b""))

        with patch("proracle.supabase_adapter.urlopen", forbidden):
            with self.assertRaises(SupabaseStoreError):
                self._store(sleeps)._request("GET", "fixtures")
        self.assertEqual(len(calls), 1)

        calls.clear()

        def always_down(request, timeout):
            calls.append(1)
            raise TimeoutError()

        with patch("proracle.supabase_adapter.urlopen", always_down):
            with self.assertRaises(SupabaseStoreError):
                self._store(sleeps, retries=2)._request("GET", "fixtures")
        self.assertEqual(len(calls), 3)

    def test_retry_bound_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            SupabaseRESTStore("https://project.example", "test-service-secret", max_retries=6)


def _rating_row(rid, fixture, *, status="FINAL", outcome="HOME_WIN", supersedes=None, retrieved="2025-01-01T20:05:00Z", home="Synthetic Home", away="Synthetic Away"):
    return {
        "id": rid,
        "fixture_id": fixture,
        "supersedes_result_id": supersedes,
        "status": status,
        "actual_outcome": outcome,
        "observed_at": retrieved,
        "source_retrieved_at": retrieved,
        "fixture": {"kickoff_at": "2025-01-01T18:00:00Z", "home_team": home, "away_team": away},
    }


class RatingResultsReadTests(unittest.TestCase):
    def _store(self, page_size: int = 500) -> SupabaseRESTStore:
        return SupabaseRESTStore(
            "https://project.example", "test-service-secret", page_size=page_size, max_retries=0
        )

    def test_reads_team_labels_through_the_fixture_join_with_exact_totals(self) -> None:
        store = self._store()
        rows = [
            _rating_row("r1", "f1"),
            _rating_row("r2", "f1", outcome="DRAW", supersedes="r1", retrieved="2025-01-02T20:05:00Z"),
            _rating_row("r3", "f2", home="Other Home", away="Other Away"),
            _rating_row("r4", "f2", status="VOID", outcome=None, supersedes="r3", retrieved="2025-01-03T09:00:00Z", home="Other Home", away="Other Away"),
        ]
        captured = []

        def request(method, table, **kwargs):
            captured.append((method, table, kwargs))
            return rows, {"Content-Range": f"0-{len(rows) - 1}/{len(rows)}"}

        store._request = request  # type: ignore[method-assign]
        results = store.rating_results()
        self.assertEqual(len(results), 1)
        only = results[0]
        self.assertEqual((only.home_team, only.away_team, only.actual_outcome), ("Synthetic Home", "Synthetic Away", "DRAW"))
        self.assertEqual(only.available_at, datetime(2025, 1, 2, 20, 5, tzinfo=timezone.utc))
        method, table, kwargs = captured[0]
        self.assertEqual((method, table, kwargs["prefer"]), ("GET", "results", "count=exact"))
        select = dict(kwargs["query"])["select"]
        self.assertIn("fixture:fixtures!results_fixture_id_fkey(kickoff_at,home_team,away_team)", select)
        self.assertNotIn("odds", select)

    def test_paginates_and_fails_closed_on_a_short_total(self) -> None:
        rows = [_rating_row(f"r{i}", f"f{i}") for i in range(3)]
        pages = iter([
            PagedResponse(rows[:2], "0-1/3"),
            PagedResponse(rows[2:], "2-2/3"),
        ])
        with patch("proracle.supabase_adapter.urlopen", lambda request, timeout: next(pages)):
            self.assertEqual(len(self._store(page_size=2).rating_results()), 3)
        short = iter([PagedResponse(rows[:1], "0-0/3")])
        with patch("proracle.supabase_adapter.urlopen", lambda request, timeout: next(short)):
            with self.assertRaises(SupabaseStoreError):
                self._store(page_size=2).rating_results()

    def test_missing_team_label_or_ambiguous_chain_fails_closed(self) -> None:
        bad_rows = [
            [_rating_row("r1", "f1", home=None)],  # type: ignore[arg-type]
            [_rating_row("r1", "f1"), _rating_row("r2", "f1")],  # two current heads
        ]
        for rows in bad_rows:
            store = self._store()
            store._request = lambda *_a, rows=rows, **_k: (rows, {"Content-Range": f"0-{len(rows) - 1}/{len(rows)}"})  # type: ignore[method-assign]
            with self.subTest(rows=len(rows)), self.assertRaises(SupabaseStoreError):
                store.rating_results()


if __name__ == "__main__":
    unittest.main()
