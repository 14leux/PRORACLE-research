"""Send a retrospective report summary to the operator's own Telegram bot.

Standalone and optional: it only reads a report that ``proracle.retrospective`` already
produced, never touches predictions, results, or the database, and a failure here cannot
affect prediction validity or result ingestion. Nothing is sent unless ``--send`` is given.
Credentials come from the process environment and never appear in output or errors.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections.abc import Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

MAX_MESSAGE_CHARS = 4096
TIMEOUT_SECONDS = 15.0
_CHAT_ID = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$")
_TOKEN = re.compile(r"^[0-9]{3,20}:[A-Za-z0-9_-]{20,100}$")

Transport = Callable[[str, bytes, float], tuple[int, bytes]]


class TelegramReportError(Exception):
    """A safe, fixed error code. It never carries the token, URL, or response text."""

    def __init__(self, error_code: str, *, retryable: bool = False):
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(error_code)


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _number(report: Mapping[str, Any], *path: str) -> float:
    node: Any = report
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            raise ValueError(f"report is missing {'.'.join(path)}")
        node = node[key]
    if isinstance(node, bool) or not isinstance(node, (int, float)) or not math.isfinite(node):
        raise ValueError(f"report field {'.'.join(path)} must be a finite number")
    return float(node)


def format_message(report: Mapping[str, Any]) -> str:
    """Plain-text summary of a retrospective report. Quality metrics only, no betting claims."""
    count = int(_number(report, "uncertainty", "sample_size"))
    identity = report.get("cohort", {}).get("model_identity", {})
    name = f"{identity.get('model_name', 'model')} {identity.get('model_version', '')}".strip()
    accuracy = report["uncertainty"]["accuracy"]
    difference = report["uncertainty"]["paired_difference_model_minus_baseline"]["log_loss"]
    lines = [
        f"PRORACLE retrospective report: {name}",
        f"Scored fixtures: {count}"
        + (" (under 30: descriptive only)" if report["uncertainty"].get("descriptive_only") else ""),
        "",
        "Accuracy (95% interval)",
        f"  model:    {_percent(accuracy['model']['estimate'])} "
        f"({_percent(accuracy['model']['lower'])} to {_percent(accuracy['model']['upper'])})",
        f"  baseline: {_percent(accuracy['historical_frequency_baseline']['estimate'])} "
        f"({_percent(accuracy['historical_frequency_baseline']['lower'])} to "
        f"{_percent(accuracy['historical_frequency_baseline']['upper'])})",
        "",
        f"Log loss: model {_number(report, 'model', 'log_loss'):.3f}, "
        f"baseline {_number(report, 'historical_frequency_baseline', 'log_loss'):.3f}",
        f"Brier: model {_number(report, 'model', 'brier_score'):.3f}, "
        f"baseline {_number(report, 'historical_frequency_baseline', 'brier_score'):.3f}",
        f"Loss difference model minus baseline: {difference['mean_difference']:+.3f} "
        f"({difference['lower']:+.3f} to {difference['upper']:+.3f}); "
        "an interval spanning zero is not evidence of improvement.",
    ]
    exclusions = report.get("exclusions") or {}
    if exclusions:
        lines += ["", "Excluded: " + ", ".join(f"{key} {value}" for key, value in sorted(exclusions.items()))]
    lines += ["", "Prediction quality only. This is not a betting or profitability claim."]
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 1] + "…"


def _urllib_transport(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        # Read and discard the body; Telegram's description is untrusted text.
        return error.code, error.read()


def send_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    transport: Transport = _urllib_transport,
    timeout: float = TIMEOUT_SECONDS,
) -> None:
    """Send one message. Raises TelegramReportError with a fixed code on any failure."""
    if not token or not chat_id:
        raise TelegramReportError("MISSING_CREDENTIALS")
    if not _TOKEN.match(token) or not _CHAT_ID.match(chat_id):
        raise TelegramReportError("MALFORMED_CREDENTIALS")
    if not text or len(text) > MAX_MESSAGE_CHARS:
        raise TelegramReportError("INVALID_MESSAGE")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": text, "disable_web_page_preview": True}).encode()
    try:
        status, payload = transport(url, body, timeout)
    except (URLError, TimeoutError, OSError):
        # URLError text can embed the request URL, and the URL contains the token.
        raise TelegramReportError("NETWORK_ERROR", retryable=True) from None
    if status == 200:
        try:
            if json.loads(payload).get("ok") is True:
                return
        except (ValueError, AttributeError):
            pass
        raise TelegramReportError("BAD_RESPONSE")
    if status == 401:
        raise TelegramReportError("UNAUTHORIZED")
    if status in (400, 403, 404):
        raise TelegramReportError("CHAT_OR_REQUEST_REJECTED")
    if status == 429:
        raise TelegramReportError("RATE_LIMITED", retryable=True)
    if status >= 500:
        raise TelegramReportError("TELEGRAM_SERVER_ERROR", retryable=True)
    raise TelegramReportError("UNEXPECTED_STATUS")


def main(argv: list[str] | None = None, *, transport: Transport = _urllib_transport) -> int:
    parser = argparse.ArgumentParser(
        description="Send a retrospective report summary to your own Telegram bot (preview unless --send)."
    )
    parser.add_argument("--report", required=True, help="JSON report from proracle.retrospective, or - for stdin")
    parser.add_argument("--send", action="store_true", help="actually send; without it the message is only printed")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read() if args.report == "-" else open(args.report, encoding="utf-8").read()
        text = format_message(json.loads(raw))
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"report error: {type(error).__name__}", file=sys.stderr)
        return 2
    if not args.send:
        print(text)
        print("\n(preview only; nothing sent. Use --send with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set.)")
        return 0
    try:
        send_message(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
            text,
            transport=transport,
        )
    except TelegramReportError as error:
        print(f"PRORACLE telegram report not sent: {error.error_code}", file=sys.stderr)
        return 3
    print("report sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
