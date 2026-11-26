"""Offline tests for proracle/ticket_mode.py (screenshot tickets) and the adapter reads it uses."""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from proracle import ticket_mode as tm
from proracle.rating import RATING_MODEL_NAME
from proracle.supabase_adapter import SupabaseRESTStore
from proracle.ticket import Candidate, DrawCounts, Ticket

NOW = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
DAY = date(2030, 1, 1)
INTAKE = "11111111-2222-4333-8444-55555555abcd"
COUNTS = DrawCounts(by_competition={"L1": (100, 40), "L2": (50, 5)}, total=1000, draws=250)


def cand(i: int, probs=(0.5, 0.3, 0.2), league="Premier League", hours=3) -> Candidate:
    home, draw, away = probs
    return Candidate(
        fixture_id=f"fx-{i:03d}",
        competition_id="L1",
        kickoff_at=NOW + timedelta(hours=hours),
        prediction_id=f"pred-{i:03d}",
        model_name=RATING_MODEL_NAME,
        home=home,
        draw=draw,
        away=away,
        competition_name=league,
    )


def shot_row(i: int, *, matched=True, odds=(2.0, 3.4, 3.8), league="Premier League") -> tm.ShotRow:
    return tm.ShotRow(
        position=i + 1,
        home_team=f"Home {i}",
        away_team=f"Away {i}",
        kickoff_at=NOW + timedelta(hours=3),
        odds=odds,
        fixture_id=f"fx-{i:03d}" if matched else None,
        competition_name=league,
    )


def probs_for(i: int):
    """Varied but valid model probabilities: some home favourites, some away, some tight."""
    table = [(0.62, 0.22, 0.16), (0.18, 0.24, 0.58), (0.40, 0.30, 0.30), (0.34, 0.32, 0.34), (0.55, 0.25, 0.20),
             (0.25, 0.28, 0.47), (0.45, 0.33, 0.22), (0.30, 0.40, 0.30), (0.52, 0.28, 0.20), (0.21, 0.26, 0.53)]
    return table[i % len(table)]


def build(n=10, *, rule=None, odds=(2.0, 3.4, 3.8), counts=COUNTS, extra_rows=(), league="Premier League"):
    rows = [shot_row(i, odds=odds, league=league) for i in range(n)] + list(extra_rows)
    cands = {f"fx-{i:03d}": cand(i, probs_for(i), league=league) for i in range(n)}
    return tm.build_shot_ticket(
        intake_id=INTAKE, ticket_date=DAY, rows=rows, candidates=cands, counts=counts, rule=rule, frozen_at=NOW
    )


def picks(result: tm.ShotResult, arm: str) -> dict[str, tm.ArmPick]:
    return {p.fixture_id: p for p in result.ticket.picks if p.arm == arm}


class RuleParsingTests(unittest.TestCase):
    def test_a_stored_structure_becomes_a_rule(self) -> None:
        rule = tm.parse_rule(
            {
                "version": 3,
                "structure": {
                    "v": 1,
                    "force_draws": {"count": 5, "basis": "draw_score"},
                    "reverse": {"count": 4, "basis": "odds_disagreement"},
                    "only_leagues": ["Premier League"],
                },
            }
        )
        self.assertEqual((rule.version, rule.force_draws, rule.reverse_count, rule.reverse_basis), (3, 5, 4, "odds_disagreement"))
        self.assertEqual(rule.only_leagues, ("Premier League",))
        self.assertIsNone(rule.skip_leagues)

    def test_anything_unexpected_stops_the_run(self) -> None:
        good = {"v": 1, "force_draws": {"count": 2, "basis": "draw_score"}}
        bad_structures = [
            "text",
            {},
            {"v": 2, "force_draws": good["force_draws"]},
            {**good, "extra": 1},
            {"v": 1, "force_draws": {"count": 0, "basis": "draw_score"}},
            {"v": 1, "force_draws": {"count": 11, "basis": "draw_score"}},
            {"v": 1, "force_draws": {"count": True, "basis": "draw_score"}},
            {"v": 1, "force_draws": {"count": 2, "basis": "margin"}},
            {"v": 1, "force_draws": {"count": 2, "basis": "draw_score", "x": 1}},
            {"v": 1, "reverse": {"count": 2, "basis": "vibes"}},
            {"v": 1, "only_leagues": ["A"], "skip_leagues": ["B"]},
            {"v": 1, "only_leagues": []},
            {"v": 1, "only_leagues": [5]},
        ]
        for structure in bad_structures:
            with self.subTest(structure=structure), self.assertRaises(tm.TicketModeError):
                tm.parse_rule({"version": 1, "structure": structure})
        with self.assertRaises(tm.TicketModeError):
            tm.parse_rule({"version": 0, "structure": good})


class ArmTests(unittest.TestCase):
    def test_devig_sums_to_one_and_keeps_the_order(self) -> None:
        p = tm.devig((2.0, 3.4, 3.8))
        self.assertAlmostEqual(sum(p), 1.0)
        self.assertGreater(p[0], p[1])
        self.assertGreater(p[1], p[2])

    def test_without_a_rule_the_ticket_has_arms_a_and_c_only(self) -> None:
        r = build()
        assert r is not None
        t = r.ticket
        self.assertEqual((t.ticket_kind, t.design_version, t.intake_id), ("SCREENSHOT", "shot-v1", INTAKE))
        self.assertEqual(t.ticket_id, tm.intake_ticket_id(INTAKE))
        self.assertEqual({p.arm for p in t.picks}, {"A", "C"})
        self.assertEqual(len(t.fixtures), 10)
        self.assertIsNone(r.arm_b_note)
        for p in t.picks:
            self.assertGreater(p.kickoff_at, t.frozen_at)

    def test_arm_a_is_the_model_pick_and_arm_c_forces_the_five_best_draw_scores(self) -> None:
        r = build()
        assert r is not None
        a, c = picks(r, "A"), picks(r, "C")
        for i in range(10):
            f = f"fx-{i:03d}"
            home, draw, away = probs_for(i)
            self.assertEqual(a[f].pick, tm.pick_outcome(home, draw, away))
            self.assertEqual(a[f].source_prediction_id, f"pred-{i:03d}")
        forced = {f for f, p in c.items() if p.forced}
        self.assertEqual(len(forced), 5)
        scores = {f: p.score or 0.0 for f, p in c.items()}
        self.assertEqual(forced, set(sorted(scores, key=lambda f: (-scores[f], f))[:5]))
        for f, p in c.items():
            self.assertEqual(p.pick, "DRAW" if p.forced else a[f].pick)

    def test_a_short_ticket_forces_at_most_as_many_draws_as_it_has_fixtures(self) -> None:
        r = build(3)
        assert r is not None
        self.assertEqual(sum(p.forced for p in picks(r, "C").values()), 3)

    def test_odds_never_change_arm_a_or_arm_c(self) -> None:
        with_odds, without = build(odds=(1.5, 4.0, 7.0)), build(odds=None)
        assert with_odds is not None and without is not None
        for arm in ("A", "C"):
            self.assertEqual(
                {f: (p.pick, p.forced, p.score) for f, p in picks(with_odds, arm).items()},
                {f: (p.pick, p.forced, p.score) for f, p in picks(without, arm).items()},
            )

    def test_rows_that_cannot_be_frozen_are_dropped_with_a_reason(self) -> None:
        started = tm.ShotRow(99, "Old", "Game", NOW - timedelta(hours=1), None, "fx-099", "Premier League")
        no_pred = tm.ShotRow(98, "No", "Prediction", NOW + timedelta(hours=4), None, "fx-098", "Premier League")
        r = tm.build_shot_ticket(
            intake_id=INTAKE,
            ticket_date=DAY,
            rows=[shot_row(0), shot_row(1, matched=False), started, no_pred],
            candidates={"fx-000": cand(0), "fx-099": cand(99, hours=-1)},
            counts=COUNTS,
            rule=None,
            frozen_at=NOW,
        )
        assert r is not None
        self.assertEqual(r.ticket.fixtures, ("fx-000",))
        self.assertEqual(
            [d.reason for d in r.dropped],
            ["not matched to a stored fixture", "already kicked off", "no pre-kickoff rating prediction"],
        )

    def test_no_ticket_when_nothing_can_be_frozen_or_there_is_no_history(self) -> None:
        self.assertIsNone(build(0))
        self.assertIsNone(build(3, counts=DrawCounts({}, 0, 0)))

    def test_the_same_inputs_give_the_same_ticket(self) -> None:
        self.assertEqual(build(rule=tm.Rule(1, force_draws=2)), build(rule=tm.Rule(1, force_draws=2)))


class ArmBTests(unittest.TestCase):
    def test_forced_draws_use_the_draw_score_and_are_marked(self) -> None:
        r = build(rule=tm.Rule(2, force_draws=3))
        assert r is not None
        b, c = picks(r, "B"), picks(r, "C")
        forced = {f for f, p in b.items() if p.adjustment == "FORCED_DRAW"}
        self.assertEqual(len(forced), 3)
        scores = {f: p.score or 0.0 for f, p in c.items()}
        self.assertEqual(forced, set(sorted(scores, key=lambda f: (-scores[f], f))[:3]))
        for f in forced:
            self.assertEqual(b[f].pick, "DRAW")
        self.assertEqual(len(b), 10)  # every fixture in scope has an arm B row
        self.assertTrue(all(p.arm_version == "rule-v2" for p in b.values()))
        a = picks(r, "A")
        for f, p in b.items():
            if p.adjustment is None:
                self.assertEqual(p.pick, a[f].pick)

    def test_reversal_by_margin_flips_the_tightest_non_draw_picks(self) -> None:
        r = build(rule=tm.Rule(1, reverse_count=3, reverse_basis="margin"))
        assert r is not None
        a, b = picks(r, "A"), picks(r, "B")
        reversed_ids = {f for f, p in b.items() if p.adjustment == "REVERSED"}
        self.assertEqual(len(reversed_ids), 3)
        margins = {}
        for i in range(10):
            f = f"fx-{i:03d}"
            top = sorted(probs_for(i), reverse=True)
            if a[f].pick != "DRAW":
                margins[f] = top[0] - top[1]
        self.assertEqual(reversed_ids, set(sorted(margins, key=lambda f: (margins[f], f))[:3]))
        for f in reversed_ids:
            self.assertNotEqual(a[f].pick, "DRAW")
            self.assertEqual(b[f].pick, "AWAY_WIN" if a[f].pick == "HOME_WIN" else "HOME_WIN")

    def test_a_draw_pick_is_never_reversed_and_a_forced_fixture_is_not_reversed_again(self) -> None:
        r = build(rule=tm.Rule(1, force_draws=4, reverse_count=10, reverse_basis="margin"))
        assert r is not None
        a, b = picks(r, "A"), picks(r, "B")
        forced = {f for f, p in b.items() if p.adjustment == "FORCED_DRAW"}
        self.assertEqual(len(forced), 4)  # a reversal can never overwrite a forced draw
        for f, p in b.items():
            if p.adjustment == "REVERSED":
                self.assertNotIn(f, forced)
                self.assertNotEqual(a[f].pick, "DRAW")

    def test_reversal_by_odds_disagreement_takes_the_largest_gaps_and_skips_rows_without_odds(self) -> None:
        odds = {i: (None if i % 4 == 3 else (1.3 + 0.35 * i, 3.4 + 0.1 * i, 9.0 - 0.6 * i)) for i in range(10)}
        rows = [shot_row(i, odds=odds[i]) for i in range(10)]  # type: ignore[arg-type]
        cands = {f"fx-{i:03d}": cand(i, probs_for(i)) for i in range(10)}
        r = tm.build_shot_ticket(
            intake_id=INTAKE, ticket_date=DAY, rows=rows, candidates=cands, counts=COUNTS,
            rule=tm.Rule(1, reverse_count=2, reverse_basis="odds_disagreement"), frozen_at=NOW,
        )
        assert r is not None
        a = picks(r, "A")
        gaps = {}
        for i in range(10):
            f = f"fx-{i:03d}"
            if odds[i] is None or a[f].pick == "DRAW":
                continue
            idx = {"HOME_WIN": 0, "DRAW": 1, "AWAY_WIN": 2}[a[f].pick]
            gaps[f] = abs(probs_for(i)[idx] - tm.devig(odds[i])[idx])  # type: ignore[arg-type]
        expected = set(sorted(gaps, key=lambda f: (-gaps[f], f))[:2])
        got = {f for f, p in picks(r, "B").items() if p.adjustment == "REVERSED"}
        self.assertEqual(got, expected)
        self.assertEqual(len(got), 2)
        for f in got:
            self.assertIn(f, r.market)

    def test_league_scope_restricts_arm_b_only(self) -> None:
        rows = [shot_row(i, league="Premier League" if i < 5 else "La Liga") for i in range(10)]
        cands = {f"fx-{i:03d}": cand(i, probs_for(i), league="Premier League" if i < 5 else "La Liga") for i in range(10)}
        for rule, expected in (
            (tm.Rule(1, force_draws=2, only_leagues=("premier league",)), {f"fx-{i:03d}" for i in range(5)}),
            (tm.Rule(1, force_draws=2, skip_leagues=("Premier League",)), {f"fx-{i:03d}" for i in range(5, 10)}),
        ):
            r = tm.build_shot_ticket(intake_id=INTAKE, ticket_date=DAY, rows=rows, candidates=cands, counts=COUNTS, rule=rule, frozen_at=NOW)
            assert r is not None
            self.assertEqual(set(picks(r, "B")), expected)
            self.assertEqual(len(picks(r, "A")), 10)  # arms A and C still cover the whole ticket
            self.assertEqual(len(picks(r, "C")), 10)

    def test_a_rule_whose_leagues_match_nothing_gives_no_arm_b_and_says_so(self) -> None:
        r = build(rule=tm.Rule(4, force_draws=2, only_leagues=("Nowhere League",)))
        assert r is not None
        self.assertEqual(picks(r, "B"), {})
        self.assertIn("no fixture is in the rule's leagues", r.arm_b_note or "")

    def test_the_note_reports_adjustments_that_could_not_all_be_applied(self) -> None:
        r = build(3, rule=tm.Rule(5, force_draws=10))
        assert r is not None
        self.assertIn("3 of 10 requested adjustments applied on 3 fixture(s)", r.arm_b_note or "")


class ReplyTests(unittest.TestCase):
    def test_reply_lists_picks_arms_market_line_and_the_calibration_notice(self) -> None:
        r = build(rule=tm.Rule(2, force_draws=2, reverse_count=1, reverse_basis="margin"), extra_rows=[shot_row(50, matched=False)])
        assert r is not None
        text = tm.format_reply(r)
        self.assertIn("Frozen before kickoff: 10 fixture(s), arms A, B, C.", text)
        self.assertIn("- Home 0 v Away 0: A home | B home | C", text)
        self.assertIn("(forced)", text)
        self.assertIn("(reversed)", text)
        self.assertIn("[A pick: model 62% vs market", text)
        self.assertIn("rule v2:", text)
        self.assertIn("1 row(s) not matched to a stored fixture are listed in the message above.", text)
        self.assertNotIn("Home 50", text)  # an unmatched row is not repeated by name
        self.assertIn("not betting advice", text)
        for word in ("stake", "bankroll", "profit"):
            self.assertNotIn(word, text.lower().replace("not betting advice and says nothing about returns", ""))

    def test_reply_lists_matched_rows_that_could_not_be_frozen_and_never_hides_how_many(self) -> None:
        def late(i: int) -> tm.ShotRow:
            return tm.ShotRow(100 + i, f"Late {i}", "Game", NOW + timedelta(hours=4), None, f"fx-9{i:02d}", "Premier League")

        extra = [late(i) for i in range(12)] + [shot_row(60, matched=False), shot_row(61, matched=False)]
        r = build(3, extra_rows=extra)
        assert r is not None
        self.assertEqual(len(r.dropped), 14)
        text = tm.format_reply(r)
        self.assertIn("2 row(s) not matched to a stored fixture are listed in the message above.", text)
        self.assertIn("Matched but not on the ticket (12):", text)
        self.assertIn("- Late 0 v Game: no pre-kickoff rating prediction", text)
        self.assertIn("- Late 9 v Game: no pre-kickoff rating prediction", text)
        self.assertNotIn("Late 10 v Game", text)
        self.assertIn("- and 2 more", text)
        # exactly ten are listed, so the total shown plus "more" equals the count
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("- Late ")), 10)
        # with nothing dropped there is no such section
        clean = build(3)
        assert clean is not None
        self.assertNotIn("not on the ticket", tm.format_reply(clean).lower())
        self.assertNotIn("not matched to a stored fixture are listed", tm.format_reply(clean))

    def test_reply_says_when_there_is_no_arm_b(self) -> None:
        r = build()
        assert r is not None
        self.assertIn("No rule is active, so there is no arm B", tm.format_reply(r))


class FakeShotStore:
    def __init__(self, rows, *, exists=False, intake=True, rule=None, candidates=None):
        self._rows, self.exists, self.intake, self.rule = rows, exists, intake, rule
        self.candidates = candidates if candidates is not None else {f"fx-{i:03d}": cand(i, probs_for(i)) for i in range(10)}
        self.appended: list[Ticket] = []

    def screenshot_ticket_exists(self, intake_id):
        return self.exists

    def intake_ticket(self, intake_id):
        return {"id": intake_id} if self.intake else None

    def intake_rows(self, intake_id):
        return self._rows

    def active_rule(self):
        return self.rule

    def earliest_predictions(self, fixture_ids, model_name):
        self.asked = (list(fixture_ids), model_name)
        return {f: c for f, c in self.candidates.items() if f in fixture_ids}

    def draw_counts(self):
        return COUNTS

    def append_ticket(self, ticket):
        self.appended.append(ticket)


def records(n=10):
    return [
        {
            "position": i + 1,
            "home_team": f"Home {i}",
            "away_team": f"Away {i}",
            "kickoff_at": "2030-01-01T12:00:00Z",
            "home_odds": 2.0,
            "draw_odds": 3.4,
            "away_odds": 3.8,
            "matched_fixture_id": f"fx-{i:03d}",
            "fixture": {"competition_name": "Premier League"},
        }
        for i in range(n)
    ]


class RunTests(unittest.TestCase):
    def test_creates_the_ticket_once_and_replies(self) -> None:
        store, sent = FakeShotStore(records()), []
        out = tm.run_ticket_mode(store, INTAKE, NOW, sent.append)
        self.assertEqual(out, {"ticket_created": 1, "ticket_exists": 0, "ticket_skipped": 0})
        self.assertEqual(len(store.appended), 1)
        self.assertEqual(store.asked[1], RATING_MODEL_NAME)
        self.assertEqual(len(store.asked[0]), 10)
        self.assertEqual(len(sent), 1)
        self.assertIn("Frozen before kickoff", sent[0])

    def test_a_rerun_for_the_same_intake_writes_nothing(self) -> None:
        store = FakeShotStore(records(), exists=True)
        sent: list[str] = []
        out = tm.run_ticket_mode(store, INTAKE, NOW, sent.append)
        self.assertEqual((out["ticket_exists"], out["ticket_created"]), (1, 0))
        self.assertEqual((store.appended, sent), ([], []))

    def test_the_active_rule_is_applied_to_arm_b(self) -> None:
        rule = {"rule_id": "r", "version": 4, "structure": {"v": 1, "force_draws": {"count": 2, "basis": "draw_score"}}}
        store = FakeShotStore(records(), rule=rule)
        tm.run_ticket_mode(store, INTAKE, NOW)
        arms = {p.arm for p in store.appended[0].picks}
        self.assertEqual(arms, {"A", "B", "C"})

    def test_bad_input_is_a_safe_error_and_nothing_is_written(self) -> None:
        for intake in ("not-a-uuid", "", INTAKE.upper(), INTAKE + "x"):
            with self.subTest(intake=intake), self.assertRaises(tm.TicketModeError):
                tm.run_ticket_mode(FakeShotStore(records()), intake, NOW)
        missing = FakeShotStore(records(), intake=False)
        with self.assertRaises(tm.TicketModeError):
            tm.run_ticket_mode(missing, INTAKE, NOW)
        bad_rule = FakeShotStore(records(), rule={"version": 1, "structure": {"v": 9}})
        with self.assertRaises(tm.TicketModeError):
            tm.run_ticket_mode(bad_rule, INTAKE, NOW)
        for store in (missing, bad_rule):
            self.assertEqual(store.appended, [])

    def test_nothing_to_freeze_is_reported_not_written(self) -> None:
        store = FakeShotStore(records(), candidates={})
        sent: list[str] = []
        out = tm.run_ticket_mode(store, INTAKE, NOW, sent.append)
        self.assertEqual((out["ticket_skipped"], out["ticket_created"]), (1, 0))
        self.assertEqual(store.appended, [])
        self.assertIn("Nothing was frozen", sent[0])

    def test_unmatched_only_rows_never_ask_the_database_for_predictions(self) -> None:
        rows = [{**r, "matched_fixture_id": None} for r in records(3)]
        store = FakeShotStore(rows)
        out = tm.run_ticket_mode(store, INTAKE, NOW)
        self.assertEqual(out["ticket_skipped"], 1)
        self.assertFalse(hasattr(store, "asked"))


class MainTests(unittest.TestCase):
    def test_missing_environment_is_a_clean_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(tm.main(["--intake-id", INTAKE], env={}), 2)
        self.assertIn("SUPABASE_URL and SUPABASE_SERVICE_KEY are required", err.getvalue())

    def test_a_run_prints_counters_and_never_the_credentials(self) -> None:
        env = {"SUPABASE_URL": "https://project.example", "SUPABASE_SERVICE_KEY": "key-placeholder", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}
        out = io.StringIO()
        with patch.object(tm, "run_ticket_mode", return_value={"ticket_created": 1, "ticket_exists": 0, "ticket_skipped": 0}), contextlib.redirect_stdout(out):
            self.assertEqual(tm.main(["--intake-id", INTAKE], env=env), 0)
        self.assertEqual(out.getvalue().strip(), "PRORACLE ticket mode complete: ticket_created=1 ticket_exists=0 ticket_skipped=0")
        self.assertNotIn("key-placeholder", out.getvalue())

    def test_a_safe_failure_exits_non_zero_and_replies(self) -> None:
        env = {"SUPABASE_URL": "https://project.example", "SUPABASE_SERVICE_KEY": "k", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}
        sent: list[str] = []
        with patch.object(tm, "run_ticket_mode", side_effect=tm.TicketModeError("no stored read has this intake id")), \
                patch("proracle.daily_summary.send_telegram", lambda token, chat, text: sent.append(text)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(tm.main(["--intake-id", INTAKE], env=env), 2)
        self.assertEqual(sent, ["I could not freeze that ticket: no stored read has this intake id."])


# --- the adapter methods this mode uses ---------------------------------------------------------------


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
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request, timeout):
        self.requests.append(request)
        return self.responses.pop(0) if self.responses else Response()


def page(rows):
    return Response(rows, {"Content-Type": "application/json", "Content-Range": f"0-{max(len(rows) - 1, 0)}/{len(rows)}"})


def store() -> SupabaseRESTStore:
    return SupabaseRESTStore("https://project.example", "test-service-secret")


class AdapterTests(unittest.TestCase):
    def test_earliest_predictions_filter_by_fixture_and_model_and_keep_the_first(self) -> None:
        def row(pid, fid, at):
            return {
                "id": pid, "fixture_id": fid, "predicted_at": at, "kickoff_at": "2030-01-01T18:00:00Z",
                "model_name": RATING_MODEL_NAME, "home_win_probability": 0.5, "draw_probability": 0.3,
                "away_win_probability": 0.2, "fixture": {"competition_id": "L1", "competition_name": "Premier League", "status": "SCHEDULED"},
            }

        rec = Recorder(page([row("p1", "f1", "2030-01-01T06:00:00Z"), row("p2", "f1", "2030-01-01T08:00:00Z"), row("p3", "f2", "2030-01-01T06:00:00Z")]))
        with patch("proracle.supabase_adapter.urlopen", rec):
            got = store().earliest_predictions(["f1", "f2"], RATING_MODEL_NAME)
        self.assertEqual(sorted(got), ["f1", "f2"])
        self.assertEqual(got["f1"].prediction_id, "p1")
        self.assertEqual(got["f1"].competition_name, "Premier League")
        url = rec.requests[0].full_url
        self.assertIn("fixture_id=in.%28f1%2Cf2%29", url.replace("(", "%28").replace(")", "%29").replace(",", "%2C"))
        self.assertIn(f"model_name=eq.{RATING_MODEL_NAME}", url)
        with self.assertRaises(ValueError):
            store().earliest_predictions([], RATING_MODEL_NAME)
        with self.assertRaises(ValueError):
            store().earliest_predictions([f"f{i}" for i in range(41)], RATING_MODEL_NAME)

    def test_intake_reads_use_the_intake_tables_and_the_rule_view(self) -> None:
        rec = Recorder(Response([{"id": INTAKE}]), page([{"position": 1}]), Response([{"id": "t"}]), Response([]), Response([{"rule_id": "r", "version": 2, "structure": {"v": 1}}]), Response([]))
        with patch("proracle.supabase_adapter.urlopen", rec):
            s = store()
            self.assertEqual(s.intake_ticket(INTAKE)["id"], INTAKE)  # type: ignore[index]
            self.assertEqual(s.intake_rows(INTAKE), [{"position": 1}])
            self.assertTrue(s.screenshot_ticket_exists(INTAKE))
            self.assertFalse(s.screenshot_ticket_exists(INTAKE))
            self.assertEqual(s.active_rule()["version"], 2)  # type: ignore[index]
            self.assertIsNone(s.active_rule())
        paths = [r.full_url.split("/rest/v1/")[1].split("?")[0] for r in rec.requests]
        self.assertEqual(paths, ["intake_tickets", "intake_ticket_rows", "tickets", "tickets", "intake_active_rule", "intake_active_rule"])
        self.assertIn("order=position.asc", rec.requests[1].full_url)
        self.assertTrue(all(r.method == "GET" for r in rec.requests))

    def test_a_screenshot_ticket_names_its_intake_and_arm_b_rows_carry_the_adjustment(self) -> None:
        r = build(rule=tm.Rule(2, force_draws=2, reverse_count=1, reverse_basis="margin"))
        assert r is not None
        rec = Recorder()
        with patch("proracle.supabase_adapter.urlopen", rec):
            store().append_ticket(r.ticket)
        head = json.loads(rec.requests[0].data)
        self.assertEqual((head["ticket_kind"], head["design_version"], head["intake_id"]), ("SCREENSHOT", "shot-v1", INTAKE))
        picks_payload = json.loads(rec.requests[2].data)
        b_rows = [p for p in picks_payload if p["arm"] == "B"]
        self.assertEqual(len(b_rows), 10)
        self.assertEqual({p["adjustment"] for p in b_rows}, {"FORCED_DRAW", "REVERSED", None})
        # arms A and C carry the key too, as null (see test_every_batch_has_uniform_keys)
        self.assertTrue(all(p["adjustment"] is None for p in picks_payload if p["arm"] in ("A", "C")))

    def test_every_batch_has_uniform_keys_because_postgrest_rejects_mixed_rows(self) -> None:
        """Found by the end-to-end run: PostgREST answers 400 PGRST102 'All object keys must match'."""
        from proracle import ticket as tk

        sim = tk.build_ticket([cand(i) for i in range(25)], COUNTS, DAY, NOW)
        shot_plain, shot_rule = build(), build(rule=tm.Rule(2, force_draws=2))
        for ticket in (sim, shot_plain and shot_plain.ticket, shot_rule and shot_rule.ticket):
            assert ticket is not None
            rec = Recorder()
            with patch("proracle.supabase_adapter.urlopen", rec):
                store().append_ticket(ticket)
            for request in rec.requests[1:]:  # ticket_fixtures and ticket_arm_picks are batches
                batch = json.loads(request.data)
                self.assertEqual(len({tuple(sorted(row)) for row in batch}), 1, request.full_url)

    def test_the_daily_simulated_ticket_payload_is_unchanged_so_it_works_without_the_intake_migration(self) -> None:
        from proracle import ticket as tk

        sim = tk.build_ticket([cand(i) for i in range(25)], COUNTS, DAY, NOW)
        assert sim is not None
        rec = Recorder()
        with patch("proracle.supabase_adapter.urlopen", rec):
            store().append_ticket(sim)
        head = json.loads(rec.requests[0].data)
        self.assertEqual(
            sorted(head),
            ["design_version", "fixture_count", "frozen_at", "id", "pool_size", "seed", "ticket_date", "ticket_kind"],
        )
        self.assertNotIn("intake_id", head)
        self.assertEqual(head["ticket_kind"], "SIMULATED")
        for p in json.loads(rec.requests[2].data):
            self.assertNotIn("adjustment", p)


class WorkflowAndMigrationTests(unittest.TestCase):
    def test_new_tables_are_allowed_and_only_for_reads_through_the_adapter(self) -> None:
        rec = Recorder()
        with patch("proracle.supabase_adapter.urlopen", rec):
            for table in ("intake_tickets", "intake_ticket_rows", "intake_active_rule"):
                store()._request("GET", table)
            with self.assertRaises(ValueError):
                store()._request("GET", "intake_audit")  # the function's audit log is not the Python side's to read
        self.assertEqual(len(rec.requests), 3)


if __name__ == "__main__":
    unittest.main()
