"""PRORACLE daily aggregate summary for Telegram.

Reads the ``proracle`` schema through PostgREST and builds ONE short plain-text message of
aggregate counts and prediction-quality aggregates (accuracy, log loss) only: no team names, no
fixture rows, no provider data. Without ``--send`` it
prints the message and exits 0. With ``--send`` it posts via the Telegram Bot API.

Environment: SUPABASE_URL, SUPABASE_SERVICE_KEY (a restricted role's token or a server key),
optionally SUPABASE_API_KEY (the public project key sent as `apikey`), and for --send
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID. Credentials and the URL are never printed. Errors
print only the error class and HTTP status, never response bodies. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime, timedelta

TIMEOUT_SECONDS = 15
MAX_ATTEMPTS = 3
STALE_HOURS = 14
PAGE_SIZE = 1000
MAX_PAGES = 20  # hard cap: 20,000 rows per list read


class SummaryError(Exception):
    """Safe error: the message holds only a class label and an optional HTTP status."""


class TransportError(Exception):
    """Raised by a transport for timeouts, connection errors, or HTTP errors."""

    def __init__(self, kind: str, status: int | None = None):
        super().__init__(kind)
        self.kind = kind
        self.status = status


def real_transport(method, url, headers, body=None, timeout=TIMEOUT_SECONDS):
    """Return (status, lower-cased headers dict, body bytes). Never exposes the URL on error."""
    if not url.startswith("https://"):
        raise TransportError("InsecureUrl")
    request = urllib.request.Request(url, data=body, method=method, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        raise TransportError("HTTPError", exc.code) from None
    except TimeoutError:
        raise TransportError("Timeout") from None
    except urllib.error.URLError as exc:
        kind = "Timeout" if isinstance(exc.reason, TimeoutError) else "URLError"
        raise TransportError(kind) from None


def _label(exc: TransportError) -> str:
    return exc.kind + (f" HTTP {exc.status}" if exc.status else "")


def _retryable(exc: TransportError) -> bool:
    return exc.kind in ("Timeout", "URLError") or (exc.status is not None and exc.status >= 500)


def request_with_retry(transport, method, url, headers, sleep=time.sleep):
    """GET/HEAD with up to MAX_ATTEMPTS tries and backoff on timeouts and 5xx."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return transport(method, url, headers)
        except TransportError as exc:
            if attempt == MAX_ATTEMPTS or not _retryable(exc):
                raise SummaryError(_label(exc)) from None
            sleep(2 ** (attempt - 1))
    raise SummaryError("Unreachable")  # pragma: no cover


def parse_content_range_count(value: str | None) -> int:
    """Parse the total from a Content-Range header such as ``0-0/1234`` or ``*/0``."""
    if not value or "/" not in value:
        raise SummaryError("BadContentRange")
    total = value.rsplit("/", 1)[1].strip()
    if not total.isdigit():
        raise SummaryError("BadContentRange")
    return int(total)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_list(body: bytes) -> list:
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SummaryError("BadJson") from None
    if not isinstance(data, list):
        raise SummaryError("BadJson")
    return data


class Reader:
    def __init__(self, base_url, key, transport=real_transport, sleep=time.sleep, api_key=None):
        self.base = base_url.rstrip("/") + "/rest/v1/"
        self.transport = transport
        self.sleep = sleep
        self.headers = {
            "apikey": api_key or key,
            "Authorization": f"Bearer {key}",
            "Accept-Profile": "proracle",
            "Accept": "application/json",
        }

    def _url(self, table, params):
        return self.base + table + "?" + urllib.parse.urlencode(params, safe=".,:()*")

    def _get(self, method, table, params, item_range):
        headers = {**self.headers, "Range-Unit": "items", "Range": item_range}
        if method == "HEAD":
            headers["Prefer"] = "count=exact"
        return request_with_retry(
            self.transport, method, self._url(table, params), headers, self.sleep
        )

    def count(self, table, filters=None):
        _, resp_headers, _ = self._get("HEAD", table, {"select": "id", **(filters or {})}, "0-0")
        return parse_content_range_count(resp_headers.get("content-range"))

    def rows(self, table, select, filters=None):
        rows: list = []
        for page in range(MAX_PAGES):
            lo = page * PAGE_SIZE
            params = {"select": select, **(filters or {})}
            _, _, body = self._get("GET", table, params, f"{lo}-{lo + PAGE_SIZE - 1}")
            chunk = _json_list(body)
            rows.extend(chunk)
            if len(chunk) < PAGE_SIZE:
                return rows
        raise SummaryError("TooManyRows")

    def newest(self, table, column):
        params = {"select": column, "order": f"{column}.desc"}
        _, _, body = self._get("GET", table, params, "0-0")
        data = _json_list(body)
        return data[0][column] if data else None


# Prediction-quality block. Model identities as stored in `predictions.model_name`
# (proracle/rating.py and proracle/baselines.py); kept as literals so this script stays standalone.
RATING_MODEL = "elo_davidson_recalibrated"
BASELINE_MODEL = "historical_frequency"
QUALITY_WINDOWS_DAYS = (7, 14)
OUTCOMES = ("HOME_WIN", "DRAW", "AWAY_WIN")
UNIFORM_LOG_LOSS = math.log(3)
MIN_PAIRED_FOR_INTERVAL = 30
PROB_FLOOR = 1e-9


def _pick(home: float, draw: float, away: float) -> str:
    """Most probable outcome; ties resolve in HOME_WIN, DRAW, AWAY_WIN order."""
    best = max(home, draw, away)
    return "HOME_WIN" if home == best else "DRAW" if draw == best else "AWAY_WIN"


def _scored(row: dict, outcome: str) -> tuple[bool, float]:
    """(pick was right, log loss) for one stored probability triple and the actual outcome."""
    probs = {
        "HOME_WIN": float(row["home_win_probability"]),
        "DRAW": float(row["draw_probability"]),
        "AWAY_WIN": float(row["away_win_probability"]),
    }
    pick = _pick(probs["HOME_WIN"], probs["DRAW"], probs["AWAY_WIN"])
    return pick == outcome, -math.log(max(probs[outcome], PROB_FLOOR))


def quality_stats(predictions: list, results: list, now: datetime) -> dict:
    """Score settled fixtures. Pure: rows in, aggregates out; no I/O and no wall clock.

    Only the earliest prediction per (fixture, model) made BEFORE kickoff counts, so a later
    cycle can never leak into the score. A result that another result supersedes is ignored.
    """
    superseded = {r.get("supersedes_result_id") for r in results if r.get("supersedes_result_id")}
    outcome_by_fixture = {
        r["fixture_id"]: r["actual_outcome"]
        for r in results
        if r.get("id") not in superseded and r.get("actual_outcome") in OUTCOMES
    }
    first: dict = {}
    for p in predictions:
        try:
            predicted_at, kickoff = _parse_ts(p["predicted_at"]), _parse_ts(p["kickoff_at"])
            key = (p["fixture_id"], p["model_name"])
            _scored(p, "DRAW")  # rejects a malformed probability triple
        except (KeyError, TypeError, ValueError):
            continue
        if predicted_at >= kickoff or p["fixture_id"] not in outcome_by_fixture:
            continue
        if key not in first or predicted_at < first[key][0]:
            first[key] = (predicted_at, kickoff, p)
    windows = {}
    for days in QUALITY_WINDOWS_DAYS:
        since = now - timedelta(days=days)
        per_model: dict = {RATING_MODEL: {}, BASELINE_MODEL: {}}
        for (fixture_id, model), (_, kickoff, row) in first.items():
            if model in per_model and since <= kickoff < now:
                per_model[model][fixture_id] = _scored(row, outcome_by_fixture[fixture_id])
        entry: dict = {}
        for model, label in ((RATING_MODEL, "rating"), (BASELINE_MODEL, "baseline")):
            scored = list(per_model[model].values())
            entry[f"{label}_n"] = len(scored)
            if scored:
                entry[f"{label}_accuracy"] = sum(hit for hit, _ in scored) / len(scored)
                entry[f"{label}_log_loss"] = sum(loss for _, loss in scored) / len(scored)
        both = per_model[RATING_MODEL].keys() & per_model[BASELINE_MODEL].keys()
        gains = [per_model[BASELINE_MODEL][f][1] - per_model[RATING_MODEL][f][1] for f in both]
        entry["paired_n"] = len(gains)
        if gains:
            mean = sum(gains) / len(gains)
            entry["gain_mean"] = mean
            if len(gains) >= MIN_PAIRED_FOR_INTERVAL:
                var = sum((g - mean) ** 2 for g in gains) / (len(gains) - 1)
                half = 1.96 * math.sqrt(var / len(gains))
                entry["gain_low"], entry["gain_high"] = mean - half, mean + half
        windows[days] = entry
    return windows


def collect_quality(reader: Reader, now: datetime) -> dict | None:
    """Best-effort quality block: a failure here must never stop the rest of the summary."""
    since = max(QUALITY_WINDOWS_DAYS)
    try:
        predictions = reader.rows(
            "predictions",
            "fixture_id,model_name,predicted_at,kickoff_at,"
            "home_win_probability,draw_probability,away_win_probability",
            {
                "and": f"(kickoff_at.gte.{iso(now - timedelta(days=since))},kickoff_at.lt.{iso(now)})",
                # Server-side filter: only the two stored model identities are ever read, so a
                # new identity can never inflate the page count or hit the row cap.
                "model_name": f"in.({RATING_MODEL},{BASELINE_MODEL})",
                "order": "id.asc",
            },
        )
        results = reader.rows(
            "results",
            "id,fixture_id,actual_outcome,supersedes_result_id",
            {
                "status": "eq.FINAL",
                "finalized_at": f"gte.{iso(now - timedelta(days=since + 1))}",
                "order": "id.asc",
            },
        )
    except SummaryError as exc:
        return {"error": str(exc)}
    return {"windows": quality_stats(predictions, results, now)}


def collect(reader: Reader, now: datetime) -> dict:
    """Fetch the aggregate numbers (the only function that touches the network)."""
    d1 = iso(now - timedelta(hours=24))
    window = {"and": f"(kickoff_at.gte.{iso(now)},kickoff_at.lt.{iso(now + timedelta(hours=48))})"}
    upcoming_ids = {r["id"] for r in reader.rows("fixtures", "id", window)}
    predicted_ids = {r["fixture_id"] for r in reader.rows("predictions", "fixture_id", window)}
    failures = reader.rows("prediction_failures", "error_code", {"occurred_at": f"gte.{d1}"})
    return {
        "fixtures_total": reader.count("fixtures"),
        "results_final_24h": reader.count(
            "results", {"status": "eq.FINAL", "finalized_at": f"gte.{d1}"}
        ),
        "predictions_24h": reader.count("predictions", {"created_at": f"gte.{d1}"}),
        "failures_by_code": dict(Counter(r.get("error_code", "UNKNOWN") for r in failures)),
        "upcoming_48h": len(upcoming_ids),
        "upcoming_with_prediction": len(upcoming_ids & predicted_ids),
        "newest_prediction_at": reader.newest("predictions", "created_at"),
        "quality": collect_quality(reader, now),
    }


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def format_quality(quality: dict | None) -> list[str]:
    """Plain-text lines for the quality block. Quality metrics only, no betting claims."""
    if not quality:
        return []
    if "error" in quality:
        return [f"Prediction quality: unavailable ({quality['error']})"]
    lines = [
        "Prediction quality (settled fixtures, predicted before kickoff)",
        "Rating model vs baseline (historical frequency); log loss: lower is better, "
        f"pure guessing is {UNIFORM_LOG_LOSS:.3f}",
    ]
    for days, w in quality["windows"].items():
        if not w.get("rating_n"):
            lines.append(f"Last {days}d: no settled fixtures yet")
            continue
        line = (
            f"Last {days}d ({w['rating_n']} fixtures): right {_pct(w['rating_accuracy'])}, "
            f"log loss {w['rating_log_loss']:.3f}"
        )
        if w.get("baseline_n"):
            line += (
                f" | baseline {_pct(w['baseline_accuracy'])}, log loss {w['baseline_log_loss']:.3f}"
            )
        lines.append(line)
        if "gain_mean" in w and "gain_low" in w:
            lines.append(
                f"  Gain over baseline: {w['gain_mean']:+.3f} "
                f"(95% interval {w['gain_low']:+.3f} to {w['gain_high']:+.3f}, n={w['paired_n']})"
            )
        elif "gain_mean" in w:
            lines.append(
                f"  Gain over baseline: {w['gain_mean']:+.3f} (too few fixtures for an interval)"
            )
    return lines


def format_message(stats: dict, now: datetime) -> str:
    """Pure formatting from already-fetched numbers; no I/O, no wall clock."""
    lines = [f"PRORACLE daily summary {now.astimezone(UTC):%Y-%m-%d %H:%M} UTC"]
    lines.append(f"Fixtures stored: {stats['fixtures_total']}")
    lines.append(f"Results (final), last 24h: {stats['results_final_24h']}")
    lines.append(f"Predictions written, last 24h: {stats['predictions_24h']}")
    failures = stats.get("failures_by_code") or {}
    if failures:
        detail = ", ".join(f"{code} {n}" for code, n in sorted(failures.items()))
        lines.append(f"Failure events, last 24h: {sum(failures.values())} ({detail})")
    else:
        lines.append("Failure events, last 24h: 0")
    lines.append(
        f"Upcoming fixtures, next 48h: {stats['upcoming_48h']} "
        f"({stats['upcoming_with_prediction']} with a prediction)"
    )
    newest = stats.get("newest_prediction_at")
    if not newest:
        lines.append("Newest prediction: none. STALE WARNING: no predictions on record")
    else:
        newest_dt = _parse_ts(newest)
        age = now - newest_dt
        stamp = f"{newest_dt:%Y-%m-%d %H:%M} UTC"
        if age > timedelta(hours=STALE_HOURS):
            hours = int(age.total_seconds() // 3600)
            lines.append(
                f"Newest prediction: {stamp}. STALE WARNING: {hours}h old (limit {STALE_HOURS}h)"
            )
        else:
            lines.append(f"Newest prediction: {stamp}")
    quality = format_quality(stats.get("quality"))
    if quality:
        lines.append("")
        lines.extend(quality)
    return "\n".join(lines)


def send_telegram(token, chat_id, text, transport=real_transport):
    """One attempt only (no retry): a retried send could post the message twice."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        status, _, _ = transport("POST", url, headers, body)
    except TransportError as exc:
        raise SummaryError(f"TelegramSend {_label(exc)}") from None
    if status != 200:
        raise SummaryError(f"TelegramSend HTTP {status}")


def main(argv=None, env=None, transport=real_transport, now=None, sleep=time.sleep) -> int:
    parser = argparse.ArgumentParser(description="PRORACLE daily aggregate summary")
    parser.add_argument("--send", action="store_true", help="send via Telegram (default: print)")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    url, key = env.get("SUPABASE_URL", ""), env.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("Error: SUPABASE_URL and SUPABASE_SERVICE_KEY are required", file=sys.stderr)
        return 2
    api_key = env.get("SUPABASE_API_KEY", "").strip() or None
    token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if args.send and not (token and chat):
        print("Error: --send needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2
    now = now or datetime.now(UTC)
    try:
        message = format_message(collect(Reader(url, key, transport, sleep, api_key), now), now)
        if args.send:
            send_telegram(token, chat, message, transport)
            print("Summary sent.")
        else:
            print(message)
    except SummaryError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    sys.exit(main())
