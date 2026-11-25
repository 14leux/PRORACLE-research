"""Offline tests for proracle/daily_summary.py. No network, no wall clock:
'now' is injected and transports are fakes."""

import contextlib
import io
import json
import math
import sys
import unittest
import urllib.parse
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


def pred(fixture, model, probs, at="2026-10-16T10:00:00Z", kickoff="2026-10-16T18:00:00Z"):
    home, draw, away = probs
    return {
        "fixture_id": fixture,
        "model_name": model,
        "predicted_at": at,
        "kickoff_at": kickoff,
        "home_win_probability": home,
        "draw_probability": draw,
        "away_win_probability": away,
    }


def final(rid, fixture, outcome, supersedes=None):
    return {
        "id": rid,
        "fixture_id": fixture,
        "actual_outcome": outcome,
        "supersedes_result_id": supersedes,
    }


class QualityTests(unittest.TestCase):
    R, B = pds.RATING_MODEL, pds.BASELINE_MODEL

    def test_scores_accuracy_log_loss_and_paired_gain(self):
        preds = [
            pred("f1", self.R, (0.6, 0.2, 0.2)),
            pred("f1", self.B, (0.4, 0.3, 0.3)),
            pred("f2", self.R, (0.2, 0.2, 0.6)),
            pred("f2", self.B, (0.4, 0.3, 0.3)),
        ]
        results = [final("r1", "f1", "HOME_WIN"), final("r2", "f2", "HOME_WIN")]
        w = pds.quality_stats(preds, results, NOW)[7]
        self.assertEqual((w["rating_n"], w["baseline_n"], w["paired_n"]), (2, 2, 2))
        self.assertAlmostEqual(w["rating_accuracy"], 0.5)
        self.assertAlmostEqual(w["baseline_accuracy"], 1.0)
        self.assertAlmostEqual(w["rating_log_loss"], (-math.log(0.6) - math.log(0.2)) / 2)
        self.assertAlmostEqual(
            w["gain_mean"],
            ((-math.log(0.4) + math.log(0.6)) + (-math.log(0.4) + math.log(0.2))) / 2,
        )
        self.assertNotIn("gain_low", w)  # two fixtures are too few for an interval

    def test_interval_needs_enough_paired_fixtures(self):
        preds, results = [], []
        for i in range(40):
            preds.append(pred(f"f{i}", self.R, (0.5, 0.25, 0.25)))
            preds.append(pred(f"f{i}", self.B, (0.4, 0.3, 0.3)))
            results.append(final(f"r{i}", f"f{i}", "HOME_WIN" if i % 3 else "AWAY_WIN"))
        w = pds.quality_stats(preds, results, NOW)[7]
        self.assertEqual(w["paired_n"], 40)
        self.assertLess(w["gain_low"], w["gain_mean"])
        self.assertLess(w["gain_mean"], w["gain_high"])

    def test_only_first_prematch_prediction_counts(self):
        preds = [
            pred("f1", self.R, (0.2, 0.2, 0.6), at="2026-10-16T09:00:00Z"),  # earliest: wrong
            pred("f1", self.R, (0.9, 0.05, 0.05), at="2026-10-16T12:00:00Z"),  # later: ignored
            pred("f1", self.R, (0.9, 0.05, 0.05), at="2026-10-16T19:00:00Z"),  # after kickoff
        ]
        w = pds.quality_stats(preds, [final("r1", "f1", "HOME_WIN")], NOW)[7]
        self.assertEqual(w["rating_n"], 1)
        self.assertAlmostEqual(w["rating_accuracy"], 0.0)

    def test_after_kickoff_unsettled_and_out_of_window_are_ignored(self):
        preds = [
            pred("late", self.R, (0.5, 0.3, 0.2), at="2026-10-16T19:00:00Z"),
            pred("open", self.R, (0.5, 0.3, 0.2)),
            pred(
                "old",
                self.R,
                (0.5, 0.3, 0.2),
                at="2026-09-01T10:00:00Z",
                kickoff="2026-09-01T18:00:00Z",
            ),
        ]
        results = [final("r1", "late", "DRAW"), final("r3", "old", "DRAW")]
        windows = pds.quality_stats(preds, results, NOW)
        self.assertEqual((windows[7]["rating_n"], windows[14]["rating_n"]), (0, 0))

    def test_superseded_and_void_results(self):
        preds = [pred("f1", self.R, (0.6, 0.2, 0.2)), pred("f2", self.R, (0.6, 0.2, 0.2))]
        results = [
            final("r1", "f1", "AWAY_WIN"),
            final("r2", "f1", "HOME_WIN", supersedes="r1"),
            final("r3", "f2", "VOID"),
        ]
        w = pds.quality_stats(preds, results, NOW)[7]
        self.assertEqual(w["rating_n"], 1)
        self.assertAlmostEqual(w["rating_accuracy"], 1.0)

    def test_malformed_rows_are_skipped(self):
        bad = [{"fixture_id": "f1"}, {**pred("f1", self.R, (0.5, 0.3, 0.2)), "kickoff_at": "x"}]
        w = pds.quality_stats(bad, [final("r1", "f1", "DRAW")], NOW)[7]
        self.assertEqual(w["rating_n"], 0)

    def test_message_block(self):
        windows = {
            7: {
                "rating_n": 400,
                "rating_accuracy": 0.485,
                "rating_log_loss": 1.0435,
                "baseline_n": 400,
                "baseline_accuracy": 0.433,
                "baseline_log_loss": 1.0723,
                "paired_n": 400,
                "gain_mean": 0.0288,
                "gain_low": 0.0,
                "gain_high": 0.0576,
            },
            14: {"rating_n": 0, "baseline_n": 0, "paired_n": 0},
        }
        msg = pds.format_message(stats(quality={"windows": windows}), NOW)
        self.assertIn("Last 7d (400 fixtures): right 48.5%, log loss 1.044", msg)
        self.assertIn("baseline 43.3%, log loss 1.072", msg)
        self.assertIn("Gain over baseline: +0.029 (95% interval +0.000 to +0.058, n=400)", msg)
        self.assertIn("Last 14d: no settled fixtures yet", msg)
        self.assertIn("pure guessing is 1.099", msg)

    def test_message_without_or_with_failed_quality(self):
        self.assertNotIn("Prediction quality", pds.format_message(stats(), NOW))
        msg = pds.format_message(stats(quality={"error": "TooManyRows"}), NOW)
        self.assertIn("Prediction quality: unavailable (TooManyRows)", msg)

    def test_prediction_read_is_filtered_to_the_two_models_on_the_server(self):
        base = routing_transport()
        seen: list[str] = []

        def transport(method, url, headers, body=None, timeout=15):
            if "/predictions?" in url and "home_win_probability" in url:
                seen.append(url)
            return base(method, url, headers, body, timeout)

        reader = pds.Reader(URL, SECRET_KEY, transport, lambda s: None)
        pds.collect_quality(reader, NOW)
        self.assertEqual(len(seen), 1)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
        self.assertEqual(query["model_name"], [f"in.({pds.RATING_MODEL},{pds.BASELINE_MODEL})"])

    def test_quality_failure_does_not_stop_the_summary(self):
        base = routing_transport()

        def transport(method, url, headers, body=None, timeout=15):
            if "/predictions?" in url and "model_name" in url:
                raise pds.TransportError("HTTPError", 400)
            return base(method, url, headers, body, timeout)

        reader = pds.Reader(URL, SECRET_KEY, transport, lambda s: None)
        got = pds.collect(reader, NOW)
        self.assertEqual(got["quality"], {"error": "HTTPError HTTP 400"})
        self.assertEqual(got["failures_by_code"], {"SOURCE_TIMEOUT": 2})


def day_preds(day, fixtures, model=pds.RATING_MODEL):
    """Pre-kickoff predictions for kickoffs on `day` ('2026-10-16'); fixtures = [(id, confidence)].
    The pick is HOME_WIN with the given confidence, the rest split evenly."""
    return [
        pred(
            fid,
            model,
            (conf, (1 - conf) / 2, (1 - conf) / 2),
            at=f"{day}T08:00:00Z",
            kickoff=f"{day}T18:00:00Z",
        )
        for fid, conf in fixtures
    ]


class ScoreboardTests(unittest.TestCase):
    R = pds.RATING_MODEL

    def board(self, preds, results, window=7):
        return pds.scoreboard_stats(preds, results, NOW)[window]

    def test_equal_confidence_ties_go_to_the_lower_fixture_id(self):
        preds = day_preds("2026-10-16", [(f"f{i}", 0.6) for i in range(1, 8)])
        results = [
            final(f"r{i}", f"f{i}", "HOME_WIN" if i <= 5 else "AWAY_WIN") for i in range(1, 8)
        ]
        w = self.board(preds, results)
        # f1..f5 are kept (all right); f6 and f7 are dropped, so their misses never count
        self.assertEqual((w["settled"], w["hits"], w["unsettled"], w["days"]), (5, 5, 0, 1))
        self.assertAlmostEqual(w["mean_stated"], 0.6)
        self.assertAlmostEqual(w["expected_hits"], 3.0)

    def test_the_five_most_confident_are_kept_per_day(self):
        confs = {"a1": 0.5, "a2": 0.9, "a3": 0.7, "a4": 0.6, "a5": 0.8, "a6": 0.55}
        preds = day_preds("2026-10-16", list(confs.items()))
        results = [final(f"r{k}", k, "HOME_WIN") for k in confs]
        w = self.board(preds, results)
        self.assertEqual(w["settled"], 5)
        self.assertAlmostEqual(w["mean_stated"], (0.9 + 0.8 + 0.7 + 0.6 + 0.55) / 5)

    def test_each_day_is_ranked_on_its_own(self):
        preds = day_preds("2026-10-15", [(f"d{i}", 0.5 + i / 100) for i in range(6)])
        preds += day_preds("2026-10-16", [(f"e{i}", 0.5 + i / 100) for i in range(6)])
        results = [final(f"r{p['fixture_id']}", p["fixture_id"], "HOME_WIN") for p in preds]
        w = self.board(preds, results)
        self.assertEqual((w["settled"], w["days"]), (10, 2))

    def test_unsettled_picks_keep_their_place_and_are_counted_apart(self):
        preds = day_preds("2026-10-16", [("u", 0.95)] + [(f"f{i}", 0.6) for i in range(1, 6)])
        results = [final(f"r{i}", f"f{i}", "HOME_WIN") for i in range(1, 6)]
        w = self.board(preds, results)
        # u (no result yet) is ranked first, so only f1..f4 fill the other slots
        self.assertEqual((w["settled"], w["unsettled"], w["hits"]), (4, 1, 4))

    def test_window_covers_complete_utc_days_only(self):
        def at(kickoff):
            return pred("x", self.R, (0.7, 0.2, 0.1), at="2026-10-09T00:00:00Z", kickoff=kickoff)

        edge = [
            ("a", at("2026-10-11T00:00:00Z"), 7),  # first instant of the oldest day: in
            ("b", at("2026-10-10T23:59:59Z"), 14),  # just before it: out of 7d, in 14d
            ("c", at("2026-10-18T03:00:00Z"), None),  # today is incomplete: in neither
        ]
        for name, row, only_from in edge:
            row = {**row, "fixture_id": name}
            results = [final("r", name, "HOME_WIN")]
            for window in (7, 14):
                got = self.board([row], results, window)["settled"]
                expected = 1 if only_from is not None and window >= only_from else 0
                self.assertEqual(got, expected, (name, window))

    def test_only_the_first_prematch_rating_prediction_counts(self):
        preds = [
            pred("f1", self.R, (0.9, 0.05, 0.05), at="2026-10-16T08:00:00Z"),
            pred("f1", self.R, (0.2, 0.3, 0.5), at="2026-10-16T12:00:00Z"),  # later cycle
            pred("f1", self.R, (0.2, 0.3, 0.5), at="2026-10-16T18:30:00Z"),  # after kickoff
            pred("f1", pds.BASELINE_MODEL, (0.99, 0.005, 0.005), at="2026-10-16T07:00:00Z"),
        ]
        w = self.board(preds, [final("r1", "f1", "HOME_WIN")])
        self.assertEqual((w["settled"], w["hits"]), (1, 1))
        self.assertAlmostEqual(w["mean_stated"], 0.9)

    def test_superseded_result_is_ignored_and_a_miss_is_counted(self):
        preds = day_preds("2026-10-16", [("f1", 0.7)])
        results = [final("r1", "f1", "HOME_WIN"), final("r2", "f1", "AWAY_WIN", supersedes="r1")]
        w = self.board(preds, results)
        self.assertEqual((w["settled"], w["hits"]), (1, 0))
        self.assertEqual(w["hit_rate"], 0.0)

    def test_malformed_rows_and_empty_input(self):
        bad_draw = {**day_preds("2026-10-16", [("f2", 0.6)])[0], "draw_probability": "x"}
        bad = [{"fixture_id": "f1"}, bad_draw]
        self.assertEqual(self.board(bad, []), {"settled": 0, "unsettled": 0, "days": 0})
        self.assertEqual(self.board([], []), {"settled": 0, "unsettled": 0, "days": 0})

    def test_message_block_has_the_label_and_the_numbers(self):
        confs = [(f"f{i}", 0.7) for i in range(1, 6)]
        preds = day_preds("2026-10-16", confs)
        results = [
            final(f"r{i}", f"f{i}", "HOME_WIN" if i <= 3 else "AWAY_WIN") for i in range(1, 6)
        ]
        board = {"windows": pds.scoreboard_stats(preds, results, NOW)}
        msg = pds.format_message(stats(scoreboard=board), NOW)
        self.assertIn("not evidence of an edge", msg)
        self.assertIn("calibration report card", msg)
        self.assertIn("Last 7 days: 3 of 5 picks right (60.0%);", msg)
        self.assertIn("stated probability averaged 70.0%", msg)
        self.assertIn("(about 4 expected)", msg)
        self.assertIn("Only 5 settled picks: too few to read much into", msg)
        self.assertIn("Last 14 days: 3 of 5 picks right", msg)

    def test_message_block_with_enough_picks_has_no_thin_note(self):
        lines = pds.format_scoreboard(
            {"windows": {7: {"settled": 30, "unsettled": 2, "days": 7, "hits": 20,
                             "hit_rate": 20 / 30, "mean_stated": 0.65, "expected_hits": 19.5}}}
        )
        text = "\n".join(lines)
        self.assertIn("20 of 30 picks right (66.7%)", text)
        self.assertIn("2 not settled yet", text)
        self.assertNotIn("too few", text)

    def test_message_block_with_no_settled_picks_and_when_absent_or_failed(self):
        thin = {"windows": {7: {"settled": 0, "unsettled": 2, "days": 1}}}
        text = "\n".join(pds.format_scoreboard(thin))
        self.assertIn("Last 7 days: no settled picks yet (2 not settled yet)", text)
        self.assertEqual(pds.format_scoreboard(None), [])
        self.assertNotIn("scoreboard", pds.format_message(stats(), NOW).lower())
        failed = pds.format_message(stats(scoreboard={"error": "TooManyRows"}), NOW)
        self.assertIn("Top-5 scoreboard: unavailable (TooManyRows)", failed)

    def test_a_failed_read_only_blanks_the_quality_blocks(self):
        base = routing_transport()

        def transport(method, url, headers, body=None, timeout=15):
            if "/predictions?" in url and "model_name" in url:
                raise pds.TransportError("HTTPError", 400)
            return base(method, url, headers, body, timeout)

        got = pds.collect(pds.Reader(URL, SECRET_KEY, transport, lambda s: None), NOW)
        self.assertEqual(got["scoreboard"], {"error": "HTTPError HTTP 400"})
        self.assertEqual(got["quality"], {"error": "HTTPError HTTP 400"})
        self.assertEqual(got["failures_by_code"], {"SOURCE_TIMEOUT": 2})
        msg = pds.format_message(got, NOW)
        self.assertIn("Top-5 scoreboard: unavailable (HTTPError HTTP 400)", msg)
        self.assertIn("Upcoming fixtures, next 48h: 3", msg)

    def test_one_read_feeds_both_blocks_and_stays_model_filtered(self):
        base = routing_transport()
        reads: list[str] = []

        def transport(method, url, headers, body=None, timeout=15):
            if "/predictions?" in url and "home_win_probability" in url:
                reads.append(url)
            return base(method, url, headers, body, timeout)

        got = pds.collect(pds.Reader(URL, SECRET_KEY, transport, lambda s: None), NOW)
        self.assertEqual(len(reads), 1)
        self.assertIn("model_name=in.(", reads[0])
        self.assertIn("windows", got["scoreboard"])
        self.assertIn("windows", got["quality"])


if __name__ == "__main__":
    unittest.main()
