"""Offline tests for proracle/daily_summary.py. No network, no wall clock:
'now' is injected and transports are fakes."""

import contextlib
import io
import json
import sys
import unittest
from datetime import UTC, datetime

from proracle import daily_summary as pds

NOW = datetime(2026, 10, 18, 5, 30, tzinfo=UTC)
SECRET_KEY = "fake" + "-svc-" + "marker-yy"
TOKEN = "fake" + "-tg-" + "marker-zz"
CHAT = "-100999888777"
URL = "https://proj-secret-ref.supabase.co"


def stats(**over):
    base = {
        "fixtures_total": 120,
        "results_final_24h": 7,
        "predictions_24h": 18,
        "failures_by_code": {},
        "upcoming_48h": 20,
        "upcoming_with_prediction": 15,
        "newest_prediction_at": "2026-10-18T03:00:00+00:00",
    }
    base.update(over)
    return base


class ParseTests(unittest.TestCase):
    def test_content_range(self):
        self.assertEqual(pds.parse_content_range_count("0-0/1234"), 1234)
        self.assertEqual(pds.parse_content_range_count("*/0"), 0)

    def test_bad_content_range(self):
        for bad in (None, "", "0-0/*", "garbage"):
            with self.assertRaises(pds.SummaryError):
                pds.parse_content_range_count(bad)


class FormatTests(unittest.TestCase):
    def test_basic_message(self):
        msg = pds.format_message(stats(), NOW)
        self.assertIn("Fixtures stored: 120", msg)
        self.assertIn("Results (final), last 24h: 7", msg)
        self.assertIn("Predictions written, last 24h: 18", msg)
        self.assertIn("Failure events, last 24h: 0", msg)
        self.assertIn("next 48h: 20 (15 with a prediction)", msg)
        self.assertIn("Newest prediction: 2026-10-18 03:00 UTC", msg)
        self.assertNotIn("STALE", msg)

    def test_failures_grouped_sorted(self):
        msg = pds.format_message(
            stats(failures_by_code={"SOURCE_TIMEOUT": 2, "MODEL_TIMEOUT": 1}), NOW
        )
        self.assertIn("Failure events, last 24h: 3 (MODEL_TIMEOUT 1, SOURCE_TIMEOUT 2)", msg)

    def test_stale_threshold(self):
        exactly = "2026-10-17T15:30:00Z"  # exactly 14h old: not stale
        over = "2026-10-17T15:29:00Z"  # 14h01m: stale
        self.assertNotIn("STALE", pds.format_message(stats(newest_prediction_at=exactly), NOW))
        msg = pds.format_message(stats(newest_prediction_at=over), NOW)
        self.assertIn("STALE WARNING: 14h old", msg)

    def test_no_predictions_is_stale(self):
        msg = pds.format_message(stats(newest_prediction_at=None), NOW)
        self.assertIn("STALE WARNING", msg)


class FakeTransport:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, method, url, headers, body=None, timeout=15):
        self.calls.append((method, url, dict(headers)))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []

    def run_retry(self, script):
        t = FakeTransport(script)
        return t, lambda: pds.request_with_retry(t, "GET", "https://x", {}, self.sleeps.append)

    def test_retries_timeout_then_succeeds(self):
        t, go = self.run_retry([pds.TransportError("Timeout"), (200, {}, b"[]")])
        self.assertEqual(go()[0], 200)
        self.assertEqual(len(t.calls), 2)
        self.assertEqual(self.sleeps, [1])

    def test_retries_5xx_then_gives_up_after_three(self):
        t, go = self.run_retry([pds.TransportError("HTTPError", 503)] * 3)
        with self.assertRaises(pds.SummaryError) as ctx:
            go()
        self.assertEqual(str(ctx.exception), "HTTPError HTTP 503")
        self.assertEqual(len(t.calls), 3)

    def test_4xx_not_retried(self):
        t, go = self.run_retry([pds.TransportError("HTTPError", 401)])
        with self.assertRaises(pds.SummaryError):
            go()
        self.assertEqual(len(t.calls), 1)

    def test_send_not_retried(self):
        t = FakeTransport([pds.TransportError("Timeout"), (200, {}, b"{}")])
        with self.assertRaises(pds.SummaryError):
            pds.send_telegram(TOKEN, CHAT, "hi", t)
        self.assertEqual(len(t.calls), 1)


class ApiKeyHeaderTest(unittest.TestCase):
    def test_separate_api_key_is_sent_as_apikey_and_token_as_bearer(self) -> None:
        seen = {}

        def transport(method, url, headers, body=None, timeout=15):
            seen.update(headers)
            return 200, {"content-range": "0-0/0"}, b"[]"

        pds.Reader(URL, "role-token", transport, api_key="project-key").count("fixtures")
        self.assertEqual(seen["apikey"], "project-key")
        self.assertEqual(seen["Authorization"], "Bearer role-token")

    def test_without_api_key_the_key_is_used_for_both(self) -> None:
        seen = {}

        def transport(method, url, headers, body=None, timeout=15):
            seen.update(headers)
            return 200, {"content-range": "0-0/0"}, b"[]"

        pds.Reader(URL, "one-key", transport).count("fixtures")
        self.assertEqual(seen["apikey"], "one-key")


def routing_transport():
    """Fake PostgREST: answers by table and method."""

    def transport(method, url, headers, body=None, timeout=15):
        assert headers["Accept-Profile"] == "proracle"
        if method == "HEAD":
            return 200, {"content-range": "0-0/5" if "results" in url else "*/0"}, b""
        if "/fixtures?" in url:
            return 200, {}, json.dumps([{"id": "a"}, {"id": "b"}, {"id": "c"}]).encode()
        if "prediction_failures" in url:
            return 200, {}, json.dumps([{"error_code": "SOURCE_TIMEOUT"}] * 2).encode()
        if "order=created_at.desc" in url:
            return 200, {}, json.dumps([{"created_at": "2026-10-18T04:00:00+00:00"}]).encode()
        return 200, {}, json.dumps([{"fixture_id": "a"}, {"fixture_id": "x"}]).encode()

    return transport


class CollectAndMainTests(unittest.TestCase):
    def test_collect(self):
        reader = pds.Reader(URL, SECRET_KEY, routing_transport(), lambda s: None)
        got = pds.collect(reader, NOW)
        self.assertEqual(got["results_final_24h"], 5)
        self.assertEqual(got["fixtures_total"], 0)
        self.assertEqual(got["failures_by_code"], {"SOURCE_TIMEOUT": 2})
        self.assertEqual((got["upcoming_48h"], got["upcoming_with_prediction"]), (3, 1))

    def test_output_never_contains_secrets_and_send_posts(self):
        posted = []
        base = routing_transport()

        def transport(method, url, headers, body=None, timeout=15):
            if method == "POST":
                posted.append((url, body))
                return 200, {}, b"{}"
            return base(method, url, headers, body, timeout)

        env = {
            "SUPABASE_URL": URL,
            "SUPABASE_SERVICE_KEY": SECRET_KEY,
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "TELEGRAM_CHAT_ID": CHAT,
        }
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc_print = pds.main([], env, transport, NOW)
            rc_send = pds.main(["--send"], env, transport, NOW)
        self.assertEqual((rc_print, rc_send), (0, 0))
        self.assertEqual(len(posted), 1)  # only --send posts
        shown = out.getvalue() + err.getvalue()
        for secret in (SECRET_KEY, TOKEN, CHAT, "proj-secret-ref"):
            self.assertNotIn(secret, shown)
        sent_text = posted[0][1].decode()
        for secret in (SECRET_KEY, TOKEN, "proj-secret-ref"):
            self.assertNotIn(secret, sent_text)

    def test_error_output_is_class_and_status_only(self):
        def transport(method, url, headers, body=None, timeout=15):
            raise pds.TransportError("HTTPError", 500)

        env = {"SUPABASE_URL": URL, "SUPABASE_SERVICE_KEY": SECRET_KEY}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = pds.main([], env, transport, NOW, lambda s: None)
        self.assertEqual(rc, 1)
        self.assertEqual(err.getvalue().strip(), "Error: HTTPError HTTP 500")
        self.assertNotIn(SECRET_KEY, err.getvalue() + out.getvalue())

    def test_send_requires_telegram_env(self):
        env = {"SUPABASE_URL": URL, "SUPABASE_SERVICE_KEY": SECRET_KEY}
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pds.main(["--send"], env, routing_transport(), NOW), 2)


if __name__ == "__main__":
    unittest.main()
