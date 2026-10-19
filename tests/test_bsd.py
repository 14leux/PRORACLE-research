"""Offline BSD adapter tests using fabricated responses only."""

from __future__ import annotations

import json
import unittest
from datetime import timezone

from proracle.bsd import BSDClient, BSDSourceError, normalize_bsd_event


def event(
    *,
    event_id: int = 41,
    status: str = "finished",
    home_score: object = 2,
    away_score: object = 1,
) -> dict[str, object]:
    return {
        "id": event_id,
        "home_team": {"id": 7, "name": "Synthetic Home"},
        "away_team": {"id": 8, "name": "Synthetic Away"},
        "league": {"id": 9, "name": "Synthetic League"},
        "event_date": "2030-01-01T18:00:00+00:00",
        "status": status,
        "home_score": home_score,
        "away_score": away_score,
        "extra_time_score": {"home": 3, "away": 1},
        "odds": {"home_win": 1.01},
        "predictions": {"home_win": 0.99},
        "raw_provider_private_field": "SENTINEL_SECRET",
    }


class FakeTransport:
    def __init__(self, *responses: tuple[int, dict[str, str], bytes]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float):
        self.calls.append((url, headers, timeout))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def json_response(payload: object) -> tuple[int, dict[str, str], bytes]:
    return 200, {"Content-Type": "application/json"}, json.dumps(payload).encode()


def live_layout_event(
    *, period: str, home: int, away: int, extra: object, shootout: object
) -> dict[str, object]:
    """Field names and structure observed on live provider responses; every value is fabricated."""
    return {
        "id": 77,
        "home_team": "Synthetic Home",
        "away_team": "Synthetic Away",
        "home_team_id": 7,
        "away_team_id": 8,
        "league_id": 9,
        "event_date": "2030-01-01T18:00:00Z",
        "status": "finished",
        "period": period,
        "current_minute": 120,
        "home_score": home,
        "away_score": away,
        "home_score_ht": 0,
        "away_score_ht": 0,
        "extra_time_score": extra,
        "penalty_shootout": shootout,
        "previous_leg_event_id": None,
    }


class RegulationScoreSemanticsTests(unittest.TestCase):
    """Observed 2026-10-18 on live responses (60 days, 5,424 finished events): home_score and
    away_score are the 90-minute score. Extra-time goals are reported separately in
    `extra_time_score`, shoot-out goals in `penalty_shootout`. Evidence: of 26 events with unequal
    extra-time goals, 21 still show a tied home_score/away_score; the 6 events with an untied score
    and an extra-time object were all second legs (`previous_leg_event_id` set). If the provider
    changes this, these tests and the probe described in docs/OPERATIONS.md are the tripwire."""

    def test_after_extra_time_outcome_is_the_ninety_minute_result(self) -> None:
        aet = live_layout_event(
            period="AET", home=2, away=2, extra={"home": 0, "away": 1}, shootout=None
        )
        self.assertEqual(normalize_bsd_event(aet).actual_outcome, "DRAW")

    def test_after_a_penalty_shootout_outcome_is_the_ninety_minute_draw(self) -> None:
        pen = live_layout_event(
            period="PEN",
            home=1,
            away=1,
            extra={"home": 0, "away": 0},
            shootout={"home": 6, "away": 7},
        )
        self.assertEqual(normalize_bsd_event(pen).actual_outcome, "DRAW")


class BSDEventNormalizationTests(unittest.TestCase):
    def test_allowlisted_fixture_and_regulation_result_ignore_extra_provider_fields(self) -> None:
        normalized = normalize_bsd_event(event())
        self.assertEqual(normalized.source_event_key, "41")
        self.assertEqual(normalized.fixture_status, "COMPLETED")
        self.assertEqual(normalized.actual_outcome, "HOME_WIN")
        self.assertEqual(normalized.kickoff_at.tzinfo, timezone.utc)
        self.assertEqual(normalized.competition_id, "9")
        self.assertNotIn("odds", normalized.__dict__)
        self.assertNotIn("raw_provider_private_field", normalized.__dict__)

    def test_non_final_status_never_creates_result_outcome(self) -> None:
        normalized = normalize_bsd_event(event(status="upcoming", home_score=None, away_score=None))
        self.assertEqual(normalized.fixture_status, "SCHEDULED")
        self.assertIsNone(normalized.actual_outcome)

    def test_provider_notstarted_status_is_a_scheduled_fixture(self) -> None:
        normalized = normalize_bsd_event(event(status="notstarted", home_score=None, away_score=None))
        self.assertEqual(normalized.fixture_status, "SCHEDULED")
        self.assertIsNone(normalized.actual_outcome)

    def test_draw_and_away_result_are_derived_from_regulation_score(self) -> None:
        self.assertEqual(normalize_bsd_event(event(home_score=1, away_score=1)).actual_outcome, "DRAW")
        self.assertEqual(normalize_bsd_event(event(home_score=0, away_score=2)).actual_outcome, "AWAY_WIN")

    def test_missing_or_malformed_required_fields_fail_closed(self) -> None:
        cases = []
        no_id = event()
        del no_id["id"]
        cases.append(no_id)
        no_team = event()
        no_team["home_team"] = {"id": 7}
        cases.append(no_team)
        no_score = event()
        del no_score["home_score"]
        cases.append(no_score)
        bool_score = event(home_score=True)
        cases.append(bool_score)
        no_timezone = event()
        no_timezone["event_date"] = "2030-01-01T18:00:00"
        cases.append(no_timezone)
        bad_status = event()
        bad_status["status"] = "in-progress"
        cases.append(bad_status)
        for value in cases:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_bsd_event(value)


class BSDClientTests(unittest.TestCase):
    def client(self, transport: FakeTransport, **kwargs: object) -> BSDClient:
        return BSDClient(
            "dummy-user-token",
            transport=transport,
            sleeper=lambda _seconds: None,
            **kwargs,
        )

    def test_upcoming_query_asks_the_provider_for_notstarted(self) -> None:
        transport = FakeTransport(json_response({"count": 1, "results": [event(status="notstarted", home_score=None, away_score=None)]}))
        matches = self.client(transport).fetch_events("upcoming", "2030-01-01", "2030-01-02")
        self.assertEqual([match.fixture_status for match in matches], ["SCHEDULED"])
        self.assertIn("status=notstarted", transport.calls[0][0])
        self.assertNotIn("status=upcoming", transport.calls[0][0])

    def test_paginates_with_bounded_v2_query_and_header_auth(self) -> None:
        transport = FakeTransport(
            json_response({"count": 3, "results": [event(event_id=1), event(event_id=2)]}),
            json_response({"count": 3, "results": [event(event_id=3)]}),
        )
        matches = self.client(transport, page_size=2).fetch_events(
            "finished", "2030-01-01", "2030-01-02"
        )
        self.assertEqual([match.source_event_key for match in matches], ["1", "2", "3"])
        self.assertEqual(len(transport.calls), 2)
        first_url, headers, timeout = transport.calls[0]
        self.assertTrue(first_url.startswith("https://sports.bzzoiro.com/api/v2/events/?"))
        self.assertIn("status=finished", first_url)
        self.assertIn("limit=2", first_url)
        self.assertIn("offset=0", first_url)
        self.assertEqual(headers["Authorization"], "Token dummy-user-token")
        self.assertNotIn("dummy-user-token", first_url)
        self.assertGreater(timeout, 0)

    def test_transient_burst_limit_retries_only_after_retry_after(self) -> None:
        sleeps: list[float] = []
        transport = FakeTransport(
            (429, {"Retry-After": "1"}, b'{"code":"rate_limited","detail":"SENTINEL_SECRET"}'),
            json_response({"count": 0, "results": []}),
        )
        client = BSDClient("dummy-user-token", transport=transport, sleeper=sleeps.append)
        self.assertEqual(client.fetch_events("upcoming", "2030-01-01", "2030-01-01"), [])
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(len(transport.calls), 2)

    def test_daily_quota_stops_without_retry_and_maps_safe_code(self) -> None:
        transport = FakeTransport(
            (429, {"Retry-After": "120"}, b'{"code":"taster_exhausted","detail":"SENTINEL_SECRET"}')
        )
        with self.assertRaises(BSDSourceError) as caught:
            self.client(transport).fetch_events("finished", "2030-01-01", "2030-01-01")
        self.assertEqual(caught.exception.error_code, "SOURCE_QUOTA_EXHAUSTED")
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("SENTINEL_SECRET", str(caught.exception))
        self.assertNotIn("dummy-user-token", str(caught.exception))

    def test_auth_failure_is_not_retried_and_body_is_not_exposed(self) -> None:
        transport = FakeTransport((401, {}, b'{"detail":"SENTINEL_SECRET"}'))
        with self.assertRaises(BSDSourceError) as caught:
            self.client(transport).fetch_events("upcoming", "2030-01-01", "2030-01-01")
        self.assertEqual(caught.exception.error_code, "SOURCE_UNAUTHORIZED")
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("SENTINEL_SECRET", str(caught.exception))

    def test_server_failure_uses_one_bounded_retry(self) -> None:
        sleeps: list[float] = []
        transport = FakeTransport(
            (503, {}, b'{"detail":"SENTINEL_SECRET"}'),
            json_response({"count": 0, "results": []}),
        )
        client = BSDClient("token", transport=transport, sleeper=sleeps.append)
        self.assertEqual(client.fetch_events("upcoming", "2030-01-01", "2030-01-01"), [])
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sleeps, [0.25])

    def test_bad_json_shape_and_duplicate_source_key_are_stable_failures(self) -> None:
        invalid = FakeTransport((200, {}, b"not-json"))
        with self.assertRaises(BSDSourceError) as bad_json:
            self.client(invalid).fetch_events("upcoming", "2030-01-01", "2030-01-01")
        self.assertEqual(bad_json.exception.error_code, "SOURCE_INVALID_RESPONSE")

        duplicate = FakeTransport(
            json_response({"count": 2, "results": [event(event_id=1)]}),
            json_response({"count": 2, "results": [event(event_id=1)]}),
        )
        with self.assertRaises(BSDSourceError) as duplicate_id:
            self.client(duplicate, page_size=1).fetch_events(
                "upcoming", "2030-01-01", "2030-01-01"
            )
        self.assertEqual(duplicate_id.exception.error_code, "SOURCE_INVALID_RESPONSE")

        incomplete = FakeTransport(
            json_response({"count": 3, "results": [event(event_id=1)]})
        )
        with self.assertRaises(BSDSourceError) as truncated:
            self.client(incomplete, page_size=2).fetch_events(
                "upcoming", "2030-01-01", "2030-01-01"
            )
        self.assertEqual(truncated.exception.error_code, "SOURCE_INVALID_RESPONSE")

    def test_unbounded_and_invalid_queries_are_rejected_before_http(self) -> None:
        transport = FakeTransport()
        client = self.client(transport)
        with self.assertRaises(ValueError):
            client.fetch_events("live", "2030-01-01", "2030-01-01")
        with self.assertRaises(ValueError):
            client.fetch_events("upcoming", "2030-01-02", "2030-01-01")
        self.assertEqual(transport.calls, [])


class InvalidFinishedRowTests(unittest.TestCase):
    """Lesson 175: one invalid finished row must not hide the valid rows in the window."""

    def fetch(self, rows: list[object], status: str = "finished", **kwargs: object):
        transport = FakeTransport(json_response({"count": len(rows), "results": rows}))
        client = BSDClient("dummy-user-token", transport=transport, sleeper=lambda _s: None, **kwargs)
        return client, client.fetch_events(status, "2030-01-01", "2030-01-02")

    def test_one_negative_score_row_is_skipped_and_counted(self) -> None:
        rows = [event(event_id=1), event(event_id=2, home_score=-1), event(event_id=3)]
        client, matches = self.fetch(rows)
        self.assertEqual([m.source_event_key for m in matches], ["1", "3"])
        self.assertEqual(client.invalid_rows_skipped, 1)

    def test_clean_window_reports_zero(self) -> None:
        client, matches = self.fetch([event(event_id=1)])
        self.assertEqual(len(matches), 1)
        self.assertEqual(client.invalid_rows_skipped, 0)

    def test_more_skips_than_the_threshold_fails_closed(self) -> None:
        # Threshold is max(3, 1% of rows seen): 3 skips pass, 4 raise.
        valid = [event(event_id=i) for i in range(10, 20)]
        bad = [event(event_id=i, away_score=-1) for i in range(1, 5)]
        client, matches = self.fetch([*valid, *bad[:3]])
        self.assertEqual((len(matches), client.invalid_rows_skipped), (10, 3))
        with self.assertRaises(BSDSourceError) as raised:
            self.fetch([*valid, *bad])
        self.assertEqual(raised.exception.error_code, "SOURCE_INVALID_RESPONSE")

    def test_invalid_upcoming_row_still_fails_the_whole_response(self) -> None:
        good = event(event_id=1, status="notstarted", home_score=None, away_score=None)
        bad = event(event_id=2, status="notstarted", home_score=None, away_score=None)
        bad["id"] = "not a valid key!"
        with self.assertRaises(BSDSourceError) as raised:
            self.fetch([good, bad], status="upcoming")
        self.assertEqual(raised.exception.error_code, "SOURCE_INVALID_RESPONSE")

    def test_duplicate_key_and_non_object_row_still_fail_closed(self) -> None:
        for rows in ([event(event_id=1), event(event_id=1)], [event(event_id=1), "not an object"]):
            with self.assertRaises(BSDSourceError) as raised:
                self.fetch(rows)
            self.assertEqual(raised.exception.error_code, "SOURCE_INVALID_RESPONSE")

    def test_no_row_content_reaches_the_error_or_the_counter(self) -> None:
        bad = event(event_id=2, home_score=-1)
        bad["home_team"] = {"name": "SENTINEL_TEAM_NAME"}
        client, _ = self.fetch([event(event_id=1), bad])
        self.assertIs(type(client.invalid_rows_skipped), int)
        rows = [event(event_id=i, home_score=-1) for i in range(1, 6)]
        rows[0]["home_team"] = {"name": "SENTINEL_TEAM_NAME"}
        with self.assertRaises(BSDSourceError) as raised:
            self.fetch(rows)
        text = f"{raised.exception!s} {raised.exception!r}"
        self.assertNotIn("SENTINEL", text)
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
