"""Offline tests for the optional Telegram report sender (no network, no real credentials)."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from uuid import UUID

from proracle.contracts import parse_prediction_record
from proracle.retrospective import ResultVersion, StoredPrediction, build_cohort, build_report
from proracle.telegram_report import (
    MAX_MESSAGE_CHARS,
    TelegramReportError,
    format_message,
    main,
    send_message,
)

BASE = datetime(2030, 1, 1, tzinfo=timezone.utc)
# Obviously fake, but shaped like a real bot token and chat id.
TOKEN = "123456789:FAKE_fake_FAKE_fake_FAKE_fake_FAKE_ab"
CHAT = "-1001234567890"


def at(days: float, hours: float = 0) -> datetime:
    return BASE + timedelta(days=days, hours=hours)


def fid(number: int) -> str:
    return str(UUID(f"00000000-0000-4000-8000-{number:012d}"))


def synthetic_report() -> dict:
    """A real report from synthetic rows, so the formatter is tested against the true shape."""
    versions = [
        ResultVersion(fid(20_000 + n), fid(n), None, "FINAL", outcome, at(n), at(n, 3))
        for n, outcome in [(1, "HOME_WIN"), (2, "DRAW"), (3, "HOME_WIN"), (10, "HOME_WIN"), (11, "AWAY_WIN")]
    ]
    predictions = []
    for n in (10, 11):
        record = parse_prediction_record(
            {
                "contract_version": "1.0",
                "prediction_id": fid(10_000 + n),
                "fixture_id": fid(n),
                "predicted_at": at(n, -6).isoformat().replace("+00:00", "Z"),
                "kickoff_at": at(n).isoformat().replace("+00:00", "Z"),
                "model": {"provider": "proracle", "name": "historical_frequency", "version": "1"},
                "prompt_version": "not_applicable",
                "input_schema_version": "features-v1",
                "input_snapshot_hash": "a" * 64,
                "probabilities": {"HOME_WIN": 0.5, "DRAW": 0.3, "AWAY_WIN": 0.2},
            }
        )
        predictions.append(StoredPrediction(record, "league-1"))
    return build_report(build_cohort(predictions, versions))


class FormatTests(unittest.TestCase):
    def test_summary_has_sample_size_intervals_and_the_claim_limit(self) -> None:
        text = format_message(synthetic_report())
        self.assertIn("Scored fixtures: 2 (under 30: descriptive only)", text)
        self.assertIn("95% interval", text)
        self.assertIn("not evidence of improvement", text)
        self.assertIn("not a betting or profitability claim", text)
        self.assertLessEqual(len(text), MAX_MESSAGE_CHARS)

    def test_malformed_report_is_rejected(self) -> None:
        with self.assertRaises((ValueError, KeyError)):
            format_message({"uncertainty": {"sample_size": 1}})


class SendTests(unittest.TestCase):
    def sent(self, status: int, payload: bytes = b'{"ok": true}'):
        calls = []

        def transport(url: str, body: bytes, timeout: float):
            calls.append((url, json.loads(body), timeout))
            return status, payload

        return transport, calls

    def test_success_posts_chat_id_and_text(self) -> None:
        transport, calls = self.sent(200)
        send_message(TOKEN, CHAT, "hello", transport=transport)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][0].endswith("/sendMessage"))
        self.assertEqual(calls[0][1]["chat_id"], CHAT)
        self.assertEqual(calls[0][1]["text"], "hello")

    def test_missing_or_malformed_credentials_never_reach_the_network(self) -> None:
        transport, calls = self.sent(200)
        for token, chat, code in [
            ("", CHAT, "MISSING_CREDENTIALS"),
            (TOKEN, "", "MISSING_CREDENTIALS"),
            ("not-a-token", CHAT, "MALFORMED_CREDENTIALS"),
            (TOKEN, "bad chat", "MALFORMED_CREDENTIALS"),
        ]:
            with self.assertRaises(TelegramReportError) as raised:
                send_message(token, chat, "hello", transport=transport)
            self.assertEqual(raised.exception.error_code, code)
        self.assertEqual(calls, [])

    def test_api_errors_map_to_fixed_codes_without_response_text(self) -> None:
        leaky = b'{"ok": false, "description": "secret detail 123456789:FAKE"}'
        for status, code, retryable in [
            (401, "UNAUTHORIZED", False),
            (400, "CHAT_OR_REQUEST_REJECTED", False),
            (403, "CHAT_OR_REQUEST_REJECTED", False),
            (429, "RATE_LIMITED", True),
            (502, "TELEGRAM_SERVER_ERROR", True),
            (302, "UNEXPECTED_STATUS", False),
        ]:
            transport, _ = self.sent(status, leaky)
            with self.assertRaises(TelegramReportError) as raised:
                send_message(TOKEN, CHAT, "hello", transport=transport)
            self.assertEqual(raised.exception.error_code, code)
            self.assertEqual(raised.exception.retryable, retryable)
            self.assertNotIn("secret detail", str(raised.exception))

    def test_ok_false_with_200_is_not_success(self) -> None:
        transport, _ = self.sent(200, b'{"ok": false}')
        with self.assertRaises(TelegramReportError) as raised:
            send_message(TOKEN, CHAT, "hello", transport=transport)
        self.assertEqual(raised.exception.error_code, "BAD_RESPONSE")

    def test_network_error_does_not_leak_the_token_bearing_url(self) -> None:
        def transport(url: str, body: bytes, timeout: float):
            raise URLError(f"cannot reach {url}")

        with self.assertRaises(TelegramReportError) as raised:
            send_message(TOKEN, CHAT, "hello", transport=transport)
        self.assertEqual(raised.exception.error_code, "NETWORK_ERROR")
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "report.json"
        self.path.write_text(json.dumps(synthetic_report()), encoding="utf-8")

    def run_main(self, argv: list[str], transport=None, env: dict[str, str] | None = None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            import os
            from unittest import mock

            with mock.patch.dict(os.environ, env or {}, clear=True):
                kwargs = {"transport": transport} if transport else {}
                code = main(argv, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_preview_prints_and_never_sends(self) -> None:
        def boom(url: str, body: bytes, timeout: float):
            raise AssertionError("preview must not send")

        code, out, _ = self.run_main(["--report", str(self.path)], boom, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT})
        self.assertEqual(code, 0)
        self.assertIn("preview only", out)
        self.assertNotIn(TOKEN, out)

    def test_send_without_credentials_fails_safely(self) -> None:
        code, out, err = self.run_main(["--report", str(self.path), "--send"])
        self.assertEqual(code, 3)
        self.assertIn("MISSING_CREDENTIALS", err)
        self.assertNotIn("sent", out)

    def test_send_success_and_failure_output_never_contains_the_token(self) -> None:
        env = {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}
        code, out, err = self.run_main(["--report", str(self.path), "--send"], lambda u, b, t: (200, b'{"ok": true}'), env)
        self.assertEqual((code, out.strip()), (0, "report sent"))
        code, out, err = self.run_main(["--report", str(self.path), "--send"], lambda u, b, t: (401, b"{}"), env)
        self.assertEqual(code, 3)
        self.assertNotIn(TOKEN, out + err)

    def test_unreadable_report_is_a_safe_error(self) -> None:
        code, _, err = self.run_main(["--report", str(self.path.with_name("missing.json"))])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)


class RealTransportTests(unittest.TestCase):
    """The urllib transport against a local stand-in server (no internet, no real bot)."""

    def serve(self, status: int, body: bytes):
        import http.server
        import threading

        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        return f"http://127.0.0.1:{server.server_port}/botX/sendMessage", seen

    def test_success_and_http_error_statuses_reach_the_caller(self) -> None:
        from proracle.telegram_report import _urllib_transport

        url, seen = self.serve(200, b'{"ok": true}')
        status, payload = _urllib_transport(url, json.dumps({"chat_id": CHAT, "text": "hi"}).encode(), 5.0)
        self.assertEqual((status, json.loads(payload)), (200, {"ok": True}))
        self.assertEqual(seen[0][1]["text"], "hi")

        url, _ = self.serve(401, b'{"ok": false, "description": "Unauthorized"}')
        status, _ = _urllib_transport(url, b"{}", 5.0)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
