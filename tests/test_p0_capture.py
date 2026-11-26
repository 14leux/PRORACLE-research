"""Offline tests for the P0 capture: the daily simulated ticket, score and country capture, the
manual score backfill, and the capture migration. No network, no wall clock, no database."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import random
import re
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from proracle import ticket as tk
from proracle.bsd import BSDMatch
from proracle.evaluate import Result
from proracle.rating import RATING_MODEL_NAME
from proracle.score_backfill import MAX_WRITE_ERRORS, main as backfill_main, run_backfill
from proracle.self_hosted import run_self_hosted
from proracle.supabase_adapter import SupabaseRESTStore, SupabaseStoreError

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261126000000_proracle_p0_capture.sql"
NOW = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
DAY = date(2030, 1, 1)


def cand(i, *, competition="L1", probs=(0.5, 0.3, 0.2), kickoff=None) -> tk.Candidate:
    home, draw, away = probs
    return tk.Candidate(
        fixture_id=f"fx-{i:03d}",
        competition_id=competition,
        kickoff_at=kickoff or NOW + timedelta(hours=3 + i % 10),
        prediction_id=f"pred-{i:03d}",
        model_name=RATING_MODEL_NAME,
        home=home,
        draw=draw,
        away=away,
    )


COUNTS = tk.DrawCounts(by_competition={"L1": (100, 40), "L2": (50, 5)}, total=1000, draws=250)


class SelectionTests(unittest.TestCase):
    def test_selection_is_the_sha256_order_and_ignores_input_order(self) -> None:
        pool = [cand(i) for i in range(30)]
        seed = tk.ticket_seed(DAY)
        expected = sorted(
            (c.fixture_id for c in pool),
            key=lambda f: (hashlib.sha256(f"{seed}:{f}".encode()).hexdigest(), f),
        )[:20]
        shuffled = pool[:]
        random.Random(1).shuffle(shuffled)
        got = [c.fixture_id for c in tk.select_fixtures(shuffled, seed)]
        self.assertEqual(got, expected)
        self.assertEqual(got, [c.fixture_id for c in tk.select_fixtures(pool, seed)])
        self.assertEqual(len(got), tk.TICKET_SIZE)

    def test_another_date_selects_another_set(self) -> None:
        pool = [cand(i) for i in range(40)]
        one = {c.fixture_id for c in tk.select_fixtures(pool, tk.ticket_seed(DAY))}
        two = {c.fixture_id for c in tk.select_fixtures(pool, tk.ticket_seed(DAY + timedelta(days=1)))}
        self.assertNotEqual(one, two)

    def test_seed_and_ticket_id_are_fixed_by_the_date(self) -> None:
        self.assertEqual(tk.ticket_seed(DAY), "proracle-sim-ticket:sim-v1:2030-01-01")
        self.assertEqual(tk.ticket_id_for(DAY), tk.ticket_id_for(DAY))
        self.assertNotEqual(tk.ticket_id_for(DAY), tk.ticket_id_for(DAY + timedelta(days=1)))


class ArmTests(unittest.TestCase):
    def test_arm_a_pick_ties_resolve_home_then_draw_then_away(self) -> None:
        self.assertEqual(tk.pick_outcome(1 / 3, 1 / 3, 1 / 3), "HOME_WIN")
        self.assertEqual(tk.pick_outcome(0.2, 0.4, 0.4), "DRAW")
        self.assertEqual(tk.pick_outcome(0.2, 0.3, 0.5), "AWAY_WIN")

    def test_draw_score_shrinks_the_competition_rate_toward_the_global_rate(self) -> None:
        self.assertAlmostEqual(tk.shrunk_draw_rate(COUNTS, "L1"), (40 + 40 * 0.25) / (100 + 40))
        self.assertAlmostEqual(tk.shrunk_draw_rate(COUNTS, "L2"), (5 + 40 * 0.25) / (50 + 40))
        # unknown or missing competition: no evidence of its own, so exactly the global rate
        self.assertAlmostEqual(tk.shrunk_draw_rate(COUNTS, "unknown"), 0.25)
        self.assertAlmostEqual(tk.shrunk_draw_rate(COUNTS, None), 0.25)
        c = cand(1, competition="L1", probs=(0.4, 0.35, 0.25))
        self.assertAlmostEqual(tk.draw_score(COUNTS, c), tk.shrunk_draw_rate(COUNTS, "L1") + 0.35)
        with self.assertRaises(ValueError):
            tk.shrunk_draw_rate(tk.DrawCounts({}, 0, 0), "L1")

    def pool(self) -> list[tk.Candidate]:
        rng = random.Random(7)
        out = []
        for i in range(30):
            draw = round(rng.uniform(0.2, 0.35), 4)
            home = round((1 - draw) * rng.uniform(0.3, 0.7), 4)
            out.append(cand(i, competition=("L1", "L2", "L3")[i % 3], probs=(home, draw, round(1 - draw - home, 4))))
        return out

    def test_ticket_has_twenty_fixtures_with_arms_a_and_c_only(self) -> None:
        ticket = tk.build_ticket(self.pool(), COUNTS, DAY, NOW)
        assert ticket is not None
        self.assertEqual(len(ticket.fixtures), 20)
        self.assertEqual(len(set(ticket.fixtures)), 20)
        self.assertEqual(len(ticket.picks), 40)
        self.assertEqual({p.arm for p in ticket.picks}, {"A", "C"})  # arm B waits for an active rule
        self.assertEqual(ticket.pool_size, 30)
        self.assertEqual(ticket.ticket_id, tk.ticket_id_for(DAY))
        self.assertEqual(ticket.design_version, "sim-v1")
        for pick in ticket.picks:
            self.assertGreater(pick.kickoff_at, ticket.frozen_at)
            if pick.arm == "A":
                self.assertTrue(pick.source_prediction_id)
                self.assertFalse(pick.forced)
                self.assertEqual(pick.arm_version, RATING_MODEL_NAME)
            else:
                self.assertIsNone(pick.source_prediction_id)
                self.assertEqual(pick.arm_version, "draw-score-v1")
                assert pick.score is not None
                self.assertTrue(0 <= pick.score <= 2)

    def test_arm_a_is_the_stored_model_pick_and_arm_c_forces_the_five_best_draw_scores(self) -> None:
        pool = self.pool()
        by_id = {c.fixture_id: c for c in pool}
        ticket = tk.build_ticket(pool, COUNTS, DAY, NOW)
        assert ticket is not None
        a = {p.fixture_id: p for p in ticket.picks if p.arm == "A"}
        c_rows = {p.fixture_id: p for p in ticket.picks if p.arm == "C"}
        for fixture_id, row in a.items():
            cc = by_id[fixture_id]
            self.assertEqual(row.pick, tk.pick_outcome(cc.home, cc.draw, cc.away))
            self.assertEqual(row.source_prediction_id, cc.prediction_id)
        scores = {f: p.score or 0.0 for f, p in c_rows.items()}
        ranked = sorted(c_rows, key=lambda f: (-scores[f], f))
        forced = {f for f, p in c_rows.items() if p.forced}
        self.assertEqual(forced, set(ranked[: tk.FORCED_DRAWS]))
        self.assertEqual(len(forced), 5)
        for fixture_id, row in c_rows.items():
            self.assertEqual(row.pick, "DRAW" if row.forced else a[fixture_id].pick)

    def test_equal_draw_scores_are_forced_in_fixture_id_order(self) -> None:
        pool = [cand(i, competition="L1", probs=(0.5, 0.3, 0.2)) for i in range(25)]
        ticket = tk.build_ticket(pool, COUNTS, DAY, NOW)
        assert ticket is not None
        forced = sorted(p.fixture_id for p in ticket.picks if p.arm == "C" and p.forced)
        self.assertEqual(forced, sorted(ticket.fixtures)[:5])

    def test_no_ticket_when_the_pool_is_short_or_history_is_missing(self) -> None:
        self.assertIsNone(tk.build_ticket(self.pool()[: tk.MIN_POOL - 1], COUNTS, DAY, NOW))
        self.assertIsNotNone(tk.build_ticket(self.pool()[: tk.MIN_POOL], COUNTS, DAY, NOW))
        self.assertIsNone(tk.build_ticket(self.pool(), tk.DrawCounts({}, 0, 0), DAY, NOW))

    def test_fixtures_that_have_already_kicked_off_are_not_in_the_pool(self) -> None:
        pool = [cand(i) for i in range(11)]
        pool[0] = cand(0, kickoff=NOW)  # kicks off now: cannot be frozen before kickoff
        pool[1] = cand(1, kickoff=NOW - timedelta(hours=1))
        self.assertIsNone(tk.build_ticket(pool, COUNTS, DAY, NOW))  # 9 left
        ticket = tk.build_ticket(pool + [cand(99)], COUNTS, DAY, NOW)
        assert ticket is not None
        self.assertEqual(ticket.pool_size, tk.MIN_POOL)
        self.assertNotIn("fx-000", ticket.fixtures)
        self.assertNotIn("fx-001", ticket.fixtures)

    def test_the_same_inputs_always_give_the_same_ticket(self) -> None:
        first = tk.build_ticket(self.pool(), COUNTS, DAY, NOW)
        second = tk.build_ticket(list(reversed(self.pool())), COUNTS, DAY, NOW)
        self.assertEqual(first, second)


class FakeTicketStore:
    def __init__(self, pool=None, *, exists=False, counts=COUNTS, fail=None):
        self.pool, self.exists, self.counts, self.fail = pool or [], exists, counts, fail
        self.pool_args = None
        self.appended: list[tk.Ticket] = []

    def ticket_exists(self, ticket_date, design_version):
        if self.fail == "exists":
            raise SupabaseStoreError()
        self.exists_args = (ticket_date, design_version)
        return self.exists

    def ticket_pool(self, after, until, model_name):
        if self.fail == "pool":
            raise SupabaseStoreError(http_status=500)
        self.pool_args = (after, until, model_name)
        return self.pool

    def draw_counts(self):
        return self.counts

    def append_ticket(self, ticket):
        if self.fail == "append":
            raise SupabaseStoreError()
        self.appended.append(ticket)


class RunDailyTicketTests(unittest.TestCase):
    def test_creates_one_ticket_from_the_next_24_hours_of_rating_predictions(self) -> None:
        store = FakeTicketStore([cand(i) for i in range(25)])
        out = tk.run_daily_ticket(store, NOW)
        self.assertEqual(out, {"ticket_created": 1, "ticket_exists": 0, "ticket_skipped": 0, "ticket_errors": 0})
        self.assertEqual(store.pool_args, (NOW, NOW + timedelta(hours=24), RATING_MODEL_NAME))
        self.assertEqual(store.exists_args, (DAY, "sim-v1"))
        self.assertEqual(len(store.appended), 1)
        self.assertEqual(store.appended[0].ticket_date, DAY)

    def test_an_existing_ticket_for_the_date_is_left_alone(self) -> None:
        store = FakeTicketStore([cand(i) for i in range(25)], exists=True)
        out = tk.run_daily_ticket(store, NOW)
        self.assertEqual((out["ticket_exists"], out["ticket_created"]), (1, 0))
        self.assertEqual(store.appended, [])
        self.assertIsNone(store.pool_args)

    def test_a_pool_below_the_minimum_skips_the_day(self) -> None:
        store = FakeTicketStore([cand(i) for i in range(tk.MIN_POOL - 1)])
        out = tk.run_daily_ticket(store, NOW)
        self.assertEqual((out["ticket_skipped"], out["ticket_created"], out["ticket_errors"]), (1, 0, 0))
        self.assertEqual(store.appended, [])

    def test_a_pool_at_the_minimum_gives_a_short_ticket_with_every_fixture(self) -> None:
        self.assertEqual(tk.MIN_POOL, 10)
        store = FakeTicketStore([cand(i) for i in range(10)])
        out = tk.run_daily_ticket(store, NOW)
        self.assertEqual((out["ticket_created"], out["ticket_skipped"]), (1, 0))
        ticket = store.appended[0]
        self.assertEqual(len(ticket.fixtures), 10)
        self.assertEqual(len(set(ticket.fixtures)), 10)
        self.assertEqual(len(ticket.picks), 20)
        self.assertEqual(ticket.pool_size, 10)
        self.assertEqual(sum(1 for p in ticket.picks if p.arm == "C" and p.forced), tk.FORCED_DRAWS)

    def test_a_pool_between_minimum_and_full_size_uses_the_whole_pool(self) -> None:
        store = FakeTicketStore([cand(i) for i in range(17)])
        tk.run_daily_ticket(store, NOW)
        self.assertEqual(len(store.appended[0].fixtures), 17)

    def test_a_pool_above_full_size_is_still_capped_at_twenty(self) -> None:
        store = FakeTicketStore([cand(i) for i in range(23)])
        tk.run_daily_ticket(store, NOW)
        self.assertEqual(len(store.appended[0].fixtures), tk.TICKET_SIZE)

    def test_a_failure_anywhere_is_counted_and_never_raised(self) -> None:
        for where in ("exists", "pool", "append"):
            with self.subTest(where=where):
                store = FakeTicketStore([cand(i) for i in range(25)], fail=where)
                out = tk.run_daily_ticket(store, NOW)
                self.assertEqual((out["ticket_errors"], out["ticket_created"]), (1, 0))

    def test_the_utc_date_names_the_ticket_even_for_a_local_clock(self) -> None:
        late = datetime(2030, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=3)))  # 20:30 UTC on Jan 1
        store = FakeTicketStore([cand(i, kickoff=late + timedelta(hours=2)) for i in range(25)])
        tk.run_daily_ticket(store, late)
        self.assertEqual(store.exists_args[0], date(2030, 1, 1))


# --- the run: capture and ticket switches ------------------------------------------------------


class FakeBSD:
    def __init__(self, upcoming, finished=(), countries=None):
        self.upcoming, self.finished = list(upcoming), list(finished)
        self.league_countries = countries or {}

    def fetch_events(self, status, _start, _end):
        return self.upcoming if status == "upcoming" else self.finished


class FakeRunStore(FakeTicketStore):
    def __init__(self, *, result_added=True, fail_scores=False, ticket_pool=None):
        super().__init__(ticket_pool or [])
        self.result_added, self.fail_scores = result_added, fail_scores
        self.fixtures: dict[str, str] = {}
        self.predictions, self.scores, self.countries_calls = [], [], []
        self.score_attempts = 0

    def ingest_match(self, match, observed_at):
        fid = self.fixtures.setdefault(
            match.source_event_key, f"00000000-0000-4000-8000-{int(match.source_event_key):012d}"
        )
        from uuid import UUID

        return UUID(fid), self.result_added and match.actual_outcome is not None

    def training_results(self):
        return [
            Result(
                fixture_id="h1",
                kickoff_at=datetime(2029, 12, 1, 18, tzinfo=timezone.utc),
                actual_outcome="HOME_WIN",
                available_at=datetime(2029, 12, 1, 20, tzinfo=timezone.utc),
            )
        ]

    def append_prediction(self, record):
        self.predictions.append(record)

    def append_failure(self, event):
        pass

    def append_match_score(self, fixture_id, home, away, observed_at):
        self.score_attempts += 1
        if self.fail_scores:
            raise SupabaseStoreError(http_status=404)
        self.scores.append((str(fixture_id), home, away))

    def append_competition_countries(self, countries, observed_at):
        self.countries_calls.append(dict(countries))


def bsd_match(key, kickoff, *, finished=False) -> BSDMatch:
    return BSDMatch(
        source_event_key=str(key),
        home_team=f"Home {key}",
        away_team=f"Away {key}",
        kickoff_at=datetime.fromisoformat(kickoff.replace("Z", "+00:00")),
        competition_id="9",
        competition_name="Synthetic League",
        fixture_status="COMPLETED" if finished else "SCHEDULED",
        home_score=2 if finished else None,
        away_score=1 if finished else None,
    )


RUN_NOW = datetime(2030, 1, 5, 9, 0, tzinfo=timezone.utc)


def soon_pool():
    """A full pool of fixtures kicking off after the run, so a ticket WOULD be built if switched on."""
    return [cand(i, kickoff=RUN_NOW + timedelta(hours=2 + i % 8)) for i in range(25)]


def run(store, bsd, **env):
    with patch.dict(os.environ, env, clear=False):
        return run_self_hosted(execute=True, bsd_client=bsd, store=store, now=RUN_NOW)


class CaptureSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in ("PRORACLE_CAPTURE_SCORES", "PRORACLE_DAILY_TICKET"):
            os.environ.pop(name, None)
        self.upcoming = [bsd_match(10, "2030-01-05T18:00:00Z")]
        self.finished = [bsd_match(20, "2030-01-01T18:00:00Z", finished=True)]

    def test_everything_is_off_by_default(self) -> None:
        store = FakeRunStore(ticket_pool=soon_pool())
        out = run(store, FakeBSD(self.upcoming, self.finished, {"9": "Kenya"}))
        self.assertEqual(store.score_attempts, 0)
        self.assertEqual(store.countries_calls, [])
        self.assertEqual(store.appended, [])
        self.assertIsNone(store.pool_args)
        self.assertEqual(out["predictions_written"], 1)
        for key in ("scores_captured", "countries_captured", "capture_errors", "ticket_created"):
            self.assertNotIn(key, out)

    def test_only_the_exact_value_one_switches_a_step_on(self) -> None:
        for value in ("", "0", "true", "yes", "2"):
            with self.subTest(value=value):
                store = FakeRunStore(ticket_pool=soon_pool())
                run(
                    store,
                    FakeBSD(self.upcoming, self.finished),
                    PRORACLE_CAPTURE_SCORES=value,
                    PRORACLE_DAILY_TICKET=value,
                )
                self.assertEqual((store.score_attempts, store.appended), (0, []))

    def test_scores_and_league_countries_are_captured_with_a_new_result(self) -> None:
        store = FakeRunStore()
        out = run(store, FakeBSD(self.upcoming, self.finished, {"9": "Kenya"}), PRORACLE_CAPTURE_SCORES="1")
        self.assertEqual(store.scores, [("00000000-0000-4000-8000-000000000020", 2, 1)])
        self.assertEqual(store.countries_calls, [{"9": "Kenya"}])
        self.assertEqual((out["scores_captured"], out["countries_captured"], out["capture_errors"]), (1, 1, 0))
        self.assertEqual(out["results_written"], 1)

    def test_a_result_that_was_already_stored_is_left_to_the_backfill(self) -> None:
        store = FakeRunStore(result_added=False)
        out = run(store, FakeBSD(self.upcoming, self.finished), PRORACLE_CAPTURE_SCORES="1")
        self.assertEqual((store.score_attempts, out["scores_captured"]), (0, 0))

    def test_capture_stops_after_repeated_write_errors_and_never_fails_the_run(self) -> None:
        finished = [bsd_match(100 + i, "2030-01-01T18:00:00Z", finished=True) for i in range(9)]
        store = FakeRunStore(fail_scores=True)
        out = run(store, FakeBSD(self.upcoming, finished, {"9": "Kenya"}), PRORACLE_CAPTURE_SCORES="1")
        self.assertEqual(store.score_attempts, 5)
        self.assertEqual((out["capture_errors"], out["scores_captured"], out["ingest_errors"]), (5, 0, 0))
        self.assertEqual(store.countries_calls, [])  # no more capture writes once the limit is hit
        self.assertEqual(out["predictions_written"], 1)

    def test_the_daily_ticket_runs_after_the_predictions_and_reports_its_counters(self) -> None:
        pool = [cand(i, kickoff=RUN_NOW + timedelta(hours=2 + i % 8)) for i in range(25)]
        store = FakeRunStore(ticket_pool=pool)
        out = run(store, FakeBSD(self.upcoming, self.finished), PRORACLE_DAILY_TICKET="1")
        self.assertEqual(out["ticket_created"], 1)
        self.assertEqual(len(store.appended), 1)
        self.assertEqual(out["predictions_written"], 1)
        self.assertNotIn("scores_captured", out)  # the score switch is independent

    def test_a_failing_ticket_never_touches_the_predictions(self) -> None:
        store = FakeRunStore(ticket_pool=soon_pool())
        store.fail = "append"
        out = run(store, FakeBSD(self.upcoming, self.finished), PRORACLE_DAILY_TICKET="1")
        self.assertEqual((out["ticket_errors"], out["ticket_created"]), (1, 0))
        self.assertEqual(out["predictions_written"], 1)

    def test_a_dry_run_never_captures_or_builds_a_ticket(self) -> None:
        store = FakeRunStore()
        with patch.dict(os.environ, {"PRORACLE_CAPTURE_SCORES": "1", "PRORACLE_DAILY_TICKET": "1"}):
            out = run_self_hosted(
                execute=False, bsd_client=FakeBSD(self.upcoming, self.finished), store=store, now=RUN_NOW
            )
        self.assertEqual((store.score_attempts, store.appended, store.predictions), (0, [], []))
        self.assertNotIn("scores_captured", out)


# --- the database adapter ------------------------------------------------------------------------


class Response:
    status = 200

    def __init__(self, payload=None, headers=None):
        self.body = b"" if payload is None else json.dumps(payload).encode()
        self.headers = headers or {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


class Recorder:
    """Stands in for urlopen: records each request and answers from a queue."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        return self.responses.pop(0) if self.responses else Response()


def store() -> SupabaseRESTStore:
    return SupabaseRESTStore("https://project.example", "test-service-secret")


def page(rows):
    return Response(rows, {"Content-Type": "application/json", "Content-Range": f"0-{max(len(rows) - 1, 0)}/{len(rows)}"})


class AdapterWriteTests(unittest.TestCase):
    def test_a_score_is_an_insert_that_keeps_an_existing_row(self) -> None:
        rec = Recorder()
        with patch("proracle.supabase_adapter.urlopen", rec):
            store().append_match_score("fx-1", 2, 1, NOW)
        request = rec.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertIn("/rest/v1/match_scores?on_conflict=fixture_id", request.full_url)
        self.assertEqual(request.get_header("Prefer"), "resolution=ignore-duplicates,return=minimal")
        self.assertEqual(
            json.loads(request.data),
            {
                "fixture_id": "fx-1",
                "home_score": 2,
                "away_score": 1,
                "source_name": "bsd",
                "observed_at": "2030-01-01T09:00:00Z",
            },
        )
        self.assertNotIn("test-service-secret", request.full_url)

    def test_countries_go_in_one_sorted_request_and_nothing_is_sent_for_none(self) -> None:
        rec = Recorder()
        with patch("proracle.supabase_adapter.urlopen", rec):
            store().append_competition_countries({}, NOW)
            self.assertEqual(rec.requests, [])
            store().append_competition_countries({"30": "Africa", "10": "Kenya"}, NOW)
        request = rec.requests[0]
        self.assertIn("/rest/v1/competition_countries?on_conflict=competition_id", request.full_url)
        self.assertEqual([r["competition_id"] for r in json.loads(request.data)], ["10", "30"])
        self.assertEqual(set(json.loads(request.data)[0]), {"competition_id", "country", "source_name", "observed_at"})

    def test_a_ticket_is_three_inserts_in_order_with_the_expected_fields(self) -> None:
        ticket = tk.build_ticket([cand(i) for i in range(25)], COUNTS, DAY, NOW)
        assert ticket is not None
        rec = Recorder()
        with patch("proracle.supabase_adapter.urlopen", rec):
            store().append_ticket(ticket)
        paths = [r.full_url.split("/rest/v1/")[1] for r in rec.requests]
        self.assertEqual(paths, ["tickets", "ticket_fixtures", "ticket_arm_picks"])
        head = json.loads(rec.requests[0].data)
        self.assertEqual(
            head,
            {
                "id": ticket.ticket_id,
                "ticket_date": "2030-01-01",
                "ticket_kind": "SIMULATED",
                "design_version": "sim-v1",
                "seed": "proracle-sim-ticket:sim-v1:2030-01-01",
                "pool_size": 25,
                "fixture_count": 20,
                "frozen_at": "2030-01-01T09:00:00Z",
            },
        )
        members = json.loads(rec.requests[1].data)
        self.assertEqual([m["position"] for m in members], list(range(1, 21)))
        self.assertEqual([m["fixture_id"] for m in members], list(ticket.fixtures))
        picks = json.loads(rec.requests[2].data)
        self.assertEqual(len(picks), 40)
        self.assertEqual({p["arm"] for p in picks}, {"A", "C"})
        self.assertTrue(all(p["frozen_at"] == "2030-01-01T09:00:00Z" for p in picks))
        self.assertTrue(all(r.get_header("Prefer") == "return=minimal" for r in rec.requests))

    def test_a_failed_ticket_insert_is_not_retried_and_stops_the_later_ones(self) -> None:
        ticket = tk.build_ticket([cand(i) for i in range(25)], COUNTS, DAY, NOW)
        assert ticket is not None
        calls = []

        def failing(request, timeout):
            calls.append(request.full_url)
            from urllib.error import HTTPError

            raise HTTPError(request.full_url, 500, "boom", {}, None)  # type: ignore[arg-type]

        with patch("proracle.supabase_adapter.urlopen", failing):
            with self.assertRaises(SupabaseStoreError):
                store().append_ticket(ticket)
        self.assertEqual(len(calls), 1)  # POST is never retried, and nothing after it was sent

    def test_the_new_tables_are_allowed_and_unknown_ones_still_are_not(self) -> None:
        rec = Recorder()
        with patch("proracle.supabase_adapter.urlopen", rec):
            for table in ("match_scores", "competition_countries", "tickets", "ticket_fixtures", "ticket_arm_picks"):
                store()._request("GET", table)
            with self.assertRaises(ValueError):
                store()._request("GET", "odds")
        self.assertEqual(len(rec.requests), 5)


class AdapterReadTests(unittest.TestCase):
    def test_ticket_exists_asks_for_the_date_kind_and_design_version(self) -> None:
        rec = Recorder(Response([{"id": "t1"}]), Response([]))
        with patch("proracle.supabase_adapter.urlopen", rec):
            self.assertTrue(store().ticket_exists(DAY, "sim-v1"))
            self.assertFalse(store().ticket_exists(DAY, "sim-v1"))
        url = rec.requests[0].full_url
        for part in ("ticket_date=eq.2030-01-01", "ticket_kind=eq.SIMULATED", "design_version=eq.sim-v1"):
            self.assertIn(part, url)

    def test_pool_uses_the_earliest_prediction_and_drops_unusable_rows(self) -> None:
        def row(pid, fid, pred_at, kickoff="2030-01-01T18:00:00Z", status="SCHEDULED", probs=(0.5, 0.3, 0.2), comp: str | None = "L1"):
            return {
                "id": pid,
                "fixture_id": fid,
                "predicted_at": pred_at,
                "kickoff_at": kickoff,
                "model_name": RATING_MODEL_NAME,
                "home_win_probability": probs[0],
                "draw_probability": probs[1],
                "away_win_probability": probs[2],
                "fixture": {"competition_id": comp, "status": status},
            }

        rows = [
            row("p1", "f1", "2030-01-01T06:00:00Z"),
            row("p2", "f1", "2030-01-01T08:00:00Z", probs=(0.1, 0.1, 0.8)),  # later prediction: ignored
            row("p3", "f2", "2030-01-01T06:00:00Z", status="LIVE"),
            row("p4", "f3", "2030-01-01T06:00:00Z", probs=(0.5, 0.5, 0.5)),  # does not sum to 1
            row("p5", "f4", "2030-01-01T19:00:00Z"),  # predicted after kickoff
            row("p6", "f5", "2030-01-01T06:00:00Z", comp=None),
        ]
        rec = Recorder(page(rows))
        with patch("proracle.supabase_adapter.urlopen", rec):
            pool = store().ticket_pool(NOW, NOW + timedelta(hours=24), RATING_MODEL_NAME)
        self.assertEqual([c.fixture_id for c in pool], ["f1", "f5"])
        self.assertEqual((pool[0].prediction_id, pool[0].home, pool[0].draw), ("p1", 0.5, 0.3))
        self.assertIsNone(pool[1].competition_id)
        url = rec.requests[0].full_url
        self.assertIn(f"model_name=eq.{RATING_MODEL_NAME}", url)
        self.assertIn("kickoff_at=gt.2030-01-01T09%3A00%3A00Z", url)
        self.assertIn("kickoff_at=lt.2030-01-02T09%3A00%3A00Z", url)

    def test_draw_counts_resolve_corrections_and_group_by_competition(self) -> None:
        def result(rid, fid, outcome, comp, supersedes=None):
            return {
                "id": rid,
                "fixture_id": fid,
                "supersedes_result_id": supersedes,
                "status": "FINAL",
                "actual_outcome": outcome,
                "observed_at": "2030-01-01T21:00:00Z",
                "source_retrieved_at": "2030-01-01T21:00:00Z",
                "fixture": {"kickoff_at": "2030-01-01T18:00:00Z", "competition_id": comp},
            }

        rows = [
            result("r1", "fa", "DRAW", "L1"),
            result("r2", "fb", "HOME_WIN", "L1"),
            result("r3", "fc", "DRAW", "L2"),
            result("r4", "fa", "HOME_WIN", "L1", supersedes="r1"),  # corrects the draw
            result("r5", "fd", "DRAW", None),
        ]
        with patch("proracle.supabase_adapter.urlopen", Recorder(page(rows))):
            counts = store().draw_counts()
        self.assertEqual((counts.total, counts.draws), (4, 2))
        self.assertEqual(dict(counts.by_competition), {"L1": (2, 0), "L2": (1, 1)})

    def test_fixture_and_score_id_lookups(self) -> None:
        rec = Recorder(page([{"id": "f1", "source_event_key": "k1"}, {"id": "f2", "source_event_key": "k2"}]), page([{"fixture_id": "f1"}]))
        with patch("proracle.supabase_adapter.urlopen", rec):
            self.assertEqual(store().fixture_ids_by_source_key(), {"k1": "f1", "k2": "f2"})
            self.assertEqual(store().score_fixture_ids(), {"f1"})
        self.assertIn("source_name=eq.bsd", rec.requests[0].full_url)


# --- the manual backfill ---------------------------------------------------------------------------


class BackfillBSD:
    def __init__(self, matches):
        self.matches, self.windows = matches, []

    def fetch_events(self, status, start, end):
        self.windows.append((status, start, end))
        return self.matches if len(self.windows) == 1 else []


class BackfillStore:
    def __init__(self, known, scored=(), fail=False):
        self.known, self.scored, self.fail = known, set(scored), fail
        self.sent = []

    def fixture_ids_by_source_key(self):
        return self.known

    def score_fixture_ids(self):
        return set(self.scored)

    def append_match_score(self, fixture_id, home, away, observed_at):
        if self.fail:
            raise SupabaseStoreError(http_status=404)
        self.sent.append((fixture_id, home, away))


def finished(key):
    return bsd_match(key, "2030-01-01T18:00:00Z", finished=True)


class BackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop("PRORACLE_ALLOW_SCORE_BACKFILL", None)

    def test_a_dry_run_only_counts_and_needs_no_database(self) -> None:
        bsd = BackfillBSD([finished(1), finished(2), bsd_match(3, "2030-01-01T18:00:00Z")])
        out = run_backfill(execute=False, days=10, bsd_client=bsd, now=RUN_NOW)  # type: ignore[arg-type]
        self.assertEqual((out["finished_events"], out["with_score"], out["scores_submitted"]), (3, 2, 0))

    def test_writing_needs_both_the_flag_and_the_environment_switch(self) -> None:
        with self.assertRaises(ValueError):
            run_backfill(execute=True, days=10, bsd_client=BackfillBSD([]), store=BackfillStore({}), now=RUN_NOW)  # type: ignore[arg-type]
        with patch.dict(os.environ, {"PRORACLE_ALLOW_SCORE_BACKFILL": "true"}):
            with self.assertRaises(ValueError):
                run_backfill(execute=True, days=10, bsd_client=BackfillBSD([]), store=BackfillStore({}), now=RUN_NOW)  # type: ignore[arg-type]

    def test_only_known_unscored_fixtures_are_written(self) -> None:
        bsd = BackfillBSD([finished(1), finished(2), finished(3)])
        db = BackfillStore({"1": "f1", "2": "f2"}, scored={"f2"})
        with patch.dict(os.environ, {"PRORACLE_ALLOW_SCORE_BACKFILL": "1"}):
            out = run_backfill(execute=True, days=10, bsd_client=bsd, store=db, now=RUN_NOW)  # type: ignore[arg-type]
        self.assertEqual(db.sent, [("f1", 2, 1)])
        self.assertEqual((out["scores_submitted"], out["already_scored"], out["fixture_missing"]), (1, 1, 1))

    def test_repeated_write_errors_stop_the_run(self) -> None:
        bsd = BackfillBSD([finished(i) for i in range(1, 20)])
        db = BackfillStore({str(i): f"f{i}" for i in range(1, 20)}, fail=True)
        with patch.dict(os.environ, {"PRORACLE_ALLOW_SCORE_BACKFILL": "1"}):
            out = run_backfill(execute=True, days=10, bsd_client=bsd, store=db, now=RUN_NOW)  # type: ignore[arg-type]
        self.assertEqual(out["write_errors"], MAX_WRITE_ERRORS)
        self.assertEqual(out["scores_submitted"], 0)

    def test_the_window_is_bounded_and_read_in_30_day_chunks(self) -> None:
        bsd = BackfillBSD([])
        run_backfill(execute=False, days=70, bsd_client=bsd, now=RUN_NOW)  # type: ignore[arg-type]
        self.assertEqual(
            bsd.windows,
            [
                ("finished", "2029-10-27", "2029-11-26"),
                ("finished", "2029-11-26", "2029-12-26"),
                ("finished", "2029-12-26", "2030-01-05"),
            ],
        )
        for days in (0, 366):
            with self.assertRaises(ValueError):
                run_backfill(execute=False, days=days, bsd_client=BackfillBSD([]), now=RUN_NOW)  # type: ignore[arg-type]

    def test_the_command_line_reports_a_missing_token_without_crashing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(backfill_main(["--days", "5"]), 2)


# --- the migration file ------------------------------------------------------------------------------


class MigrationFileTests(unittest.TestCase):
    sql = MIGRATION.read_text(encoding="utf-8")
    lowered = sql.lower()

    def test_every_table_is_created_only_if_missing_and_all_five_exist(self) -> None:
        names = ("match_scores", "competition_countries", "tickets", "ticket_fixtures", "ticket_arm_picks")
        for name in names:
            self.assertIn(f"create table if not exists proracle.{name}", self.lowered)
        self.assertEqual(self.lowered.count("create table"), self.lowered.count("create table if not exists"))
        self.assertEqual(self.lowered.count("create table"), 5)

    def test_it_can_never_be_destructive_or_grant_more_than_select_and_insert(self) -> None:
        for forbidden in ("drop table", "drop schema", "truncate", "grant update", "grant delete", "grant all", "alter role"):
            self.assertNotIn(forbidden, self.lowered, forbidden)
        self.assertIn("grant select, insert on table", self.lowered)
        self.assertIn("revoke all privileges on table", self.lowered)
        self.assertLess(self.lowered.index("revoke all privileges"), self.lowered.index("grant select, insert"))

    def test_the_freeze_before_kickoff_and_one_ticket_per_day_rules_are_in_the_schema(self) -> None:
        self.assertIn("check (frozen_at < kickoff_at)", self.lowered)
        self.assertIn("unique (ticket_date, ticket_kind, design_version)", self.lowered)
        self.assertIn("unique (ticket_id, fixture_id, arm)", self.lowered)
        self.assertIn("check (arm in ('a', 'b', 'c'))", self.lowered)

    def test_policies_are_created_only_when_missing_and_cover_select_and_insert_only(self) -> None:
        self.assertIn("if not exists (", self.lowered)
        self.assertEqual(self.lowered.count("for select to proracle_runtime"), 1)
        self.assertEqual(self.lowered.count("for insert to proracle_runtime"), 1)
        self.assertNotIn("for update", self.lowered)
        self.assertNotIn("for delete", self.lowered)

    def test_no_odds_stake_or_image_column_exists(self) -> None:
        lines = [line for line in self.sql.splitlines() if not line.lstrip().startswith("--")]
        types = "uuid|text|date|smallint|integer|boolean|numeric|timestamptz"
        columns = re.findall(rf"^\s{{4}}([a-z_]+)\s+(?:{types})", "\n".join(lines), re.M)
        self.assertGreater(len(columns), 20)
        for column in columns:
            for word in ("odds", "price", "stake", "bankroll", "image", "screenshot", "payload", "raw"):
                self.assertNotIn(word, column)


if __name__ == "__main__":
    unittest.main()
