"""Offline tests for the review workflow's code (proracle/review.py), the ticket times, and slate scoring."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from proracle import review as rv
from proracle import slate as sl
from proracle import ticket_mode as tm
from proracle.rating import RATING_MODEL_NAME
from proracle.supabase_adapter import SupabaseRESTStore
from proracle.ticket import REVIEW_WAIT, Candidate, DrawCounts, utc_and_eat

UTC = timezone.utc
FROZEN = datetime(2030, 1, 1, 9, 0, tzinfo=UTC)
KICKOFF = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)  # the last kickoff of every fake ticket
DUE = KICKOFF + REVIEW_WAIT  # 2030-01-03 00:00 UTC
BEFORE_DUE = datetime(2030, 1, 2, 12, 0, tzinfo=UTC)
AFTER_DUE = datetime(2030, 1, 3, 6, 0, tzinfo=UTC)
ACTIVATION = datetime(2030, 1, 1, 0, 0, tzinfo=UTC)
OUT = {"H": "HOME_WIN", "D": "DRAW", "A": "AWAY_WIN"}


def tid(i: int) -> str:
    return f"{i:08d}-0000-4000-8000-000000000000"


def make_ticket(i: int, *, frozen: datetime = FROZEN, due: datetime | None = DUE, kickoff: datetime = KICKOFF, n: int = 4) -> dict:
    """Four fixtures; arm A picks home/home/draw/away; arm C forces a draw on the first; rule 3's arm B reverses the second."""
    t = tid(i)
    fixtures = [f"{t}:fx{k}" for k in range(n)]
    a = ["HOME_WIN", "HOME_WIN", "DRAW", "AWAY_WIN"] * ((n + 3) // 4)
    picks = []
    for k, f in enumerate(fixtures):
        base = {"fixture_id": f, "kickoff_at": kickoff.isoformat()}
        picks.append({**base, "arm": "A", "arm_version": "rating-v1", "pick": a[k], "forced": False, "adjustment": None, "source_prediction_id": f"pred-{i}-{k}"})
        picks.append({**base, "arm": "C", "arm_version": "draw-score-v1", "pick": "DRAW" if k == 0 else a[k], "forced": k == 0, "adjustment": None, "source_prediction_id": None})
        picks.append({**base, "arm": "B", "arm_version": "rule-v3", "pick": "AWAY_WIN" if k == 1 else a[k], "forced": False, "adjustment": "REVERSED" if k == 1 else None, "source_prediction_id": None})
    return {
        "header": {
            "id": t, "intake_id": f"{i:08d}-1111-4000-8000-000000000000", "frozen_at": frozen.isoformat(),
            "fixture_count": n, "last_kickoff_at": None if due is None else kickoff.isoformat(),
            "review_due_at": None if due is None else due.isoformat(),
        },
        "inputs": {
            "fixtures": [
                {"fixture_id": f, "position": k + 1, "fixture": {"home_team": f"Home {k}", "away_team": f"Away {k}", "kickoff_at": kickoff.isoformat()}}
                for k, f in enumerate(fixtures)
            ],
            "picks": picks,
        },
        "fixtures": fixtures,
    }


class FakeReviewStore:
    def __init__(self, tickets, *, outcomes=None, fail_append=False, reviewed=()):
        self.tickets = {t["header"]["id"]: t for t in tickets}
        self.outcomes = dict(outcomes or {})
        self.reviewed = set(reviewed)
        self.appended: list[tuple] = []
        self.fail_append = fail_append
        self.asked_after: list[datetime] = []

    def settle(self, ticket: dict, results: str, upto: int | None = None) -> None:
        for k, f in enumerate(ticket["fixtures"][:upto]):
            self.outcomes[f] = OUT[results[k]]

    def reviewable_tickets(self, frozen_after):
        self.asked_after.append(frozen_after)  # the database filters on this, exactly as here
        rows = [t["header"] for t in self.tickets.values() if datetime.fromisoformat(t["header"]["frozen_at"]) > frozen_after]
        return sorted(rows, key=lambda h: (h["frozen_at"], h["id"]))

    def reviewed_ticket_ids(self):
        return set(self.reviewed)

    def ticket_review_inputs(self, ticket_id):
        return self.tickets[ticket_id]["inputs"]

    def current_outcomes(self, ids):
        return {f: self.outcomes[f] for f in ids if f in self.outcomes}

    def stored_scores(self, ids):
        return {f: (2, 1) for f in ids if self.outcomes.get(f) == "HOME_WIN"}

    def prediction_probabilities(self, ids):
        return {p: (0.5, 0.3, 0.2) for p in ids}

    def intake_rows(self, intake_id):
        t = next(t for t in self.tickets.values() if t["header"]["intake_id"] == intake_id)
        return [
            {"matched_fixture_id": f, "home_odds": 2.0, "draw_odds": 3.4, "away_odds": 3.8} if k < 3 else
            {"matched_fixture_id": f, "home_odds": None, "draw_odds": None, "away_odds": None}
            for k, f in enumerate(t["fixtures"])
        ]

    def append_review(self, ticket_id, completeness, picks_total, picks_settled, summary):
        if self.fail_append:
            raise RuntimeError("database down")
        if ticket_id in self.reviewed:
            raise RuntimeError("a second review for the same ticket")
        self.reviewed.add(ticket_id)
        self.appended.append((ticket_id, completeness, picks_total, picks_settled, dict(summary)))


def run(store, now=AFTER_DUE, *, dry=False, after=ACTIVATION, cap=3):
    sent: list[str] = []
    shown: list[str] = []
    summary = rv.run_review(
        store, now=now, frozen_after=after, send=None if dry else sent.append, dry_run=dry,
        show=shown.append, max_reviews=cap,
    )
    return summary, sent, shown


class DueTests(unittest.TestCase):
    def test_a_fully_settled_ticket_is_reviewed_before_the_due_time(self) -> None:
        t = make_ticket(1)
        store = FakeReviewStore([t])
        store.settle(t, "HHDA")
        summary, sent, _ = run(store, BEFORE_DUE)
        self.assertEqual((summary["sent"], summary["not_due"]), (1, 0))
        self.assertEqual(len(sent), 1)

    def test_an_unsettled_ticket_waits_until_36_hours_after_its_last_kickoff(self) -> None:
        t = make_ticket(1)
        store = FakeReviewStore([t])
        store.settle(t, "HH", upto=2)
        summary, sent, _ = run(store, BEFORE_DUE)
        self.assertEqual((summary["sent"], summary["not_due"], sent), (0, 1, []))
        summary, sent, _ = run(store, AFTER_DUE)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(store.appended[0][1], "PARTIAL")

    def test_the_due_time_itself_counts_as_due(self) -> None:
        store = FakeReviewStore([make_ticket(1)])
        summary, _, _ = run(store, DUE)
        self.assertEqual(summary["sent"], 1)

    def test_an_older_ticket_without_a_stored_due_time_falls_back_to_its_latest_pick_kickoff(self) -> None:
        old = make_ticket(1, due=None)
        store = FakeReviewStore([old])
        self.assertEqual(run(store, BEFORE_DUE)[0]["not_due"], 1)
        self.assertEqual(run(store, AFTER_DUE)[0]["sent"], 1)

    def test_the_wait_is_exactly_36_hours_and_the_freeze_stores_it(self) -> None:
        self.assertEqual(REVIEW_WAIT, timedelta(hours=36))
        self.assertTrue(rv.is_due(all_settled=False, due_at=DUE, now=DUE))
        self.assertFalse(rv.is_due(all_settled=False, due_at=DUE, now=DUE - timedelta(seconds=1)))
        self.assertTrue(rv.is_due(all_settled=True, due_at=None, now=FROZEN))
        self.assertFalse(rv.is_due(all_settled=False, due_at=None, now=AFTER_DUE))  # unknown time: wait for results

    def test_an_incomplete_append_is_skipped_never_reviewed(self) -> None:
        t = make_ticket(1)
        t["header"]["fixture_count"] = 5  # a ticket whose later requests did not finish
        store = FakeReviewStore([t])
        summary, sent, _ = run(store)
        self.assertEqual((summary["incomplete"], summary["sent"], sent), (1, 0, []))


class OnceTests(unittest.TestCase):
    def test_a_rerun_sends_nothing_a_second_time(self) -> None:
        t = make_ticket(1)
        store = FakeReviewStore([t])
        store.settle(t, "HHDA")
        first, sent1, _ = run(store)
        second, sent2, _ = run(store)
        self.assertEqual((first["sent"], second["sent"]), (1, 0))
        self.assertEqual(len(sent1), 1)
        self.assertEqual(sent2, [])
        self.assertEqual(len(store.appended), 1)

    def test_a_ticket_with_a_stored_review_is_skipped(self) -> None:
        t = make_ticket(1)
        store = FakeReviewStore([t], reviewed=[t["header"]["id"]])
        summary, sent, _ = run(store)
        self.assertEqual((summary["considered"], sent), (0, []))

    def test_when_the_review_cannot_be_recorded_the_run_stops_at_once_with_no_further_send(self) -> None:
        store = FakeReviewStore([make_ticket(1), make_ticket(2, frozen=FROZEN + timedelta(hours=1))], fail_append=True)
        sent: list[str] = []
        with self.assertRaises(rv.ReviewError):
            rv.run_review(store, now=AFTER_DUE, frozen_after=ACTIVATION, send=sent.append, dry_run=False)
        self.assertEqual(len(sent), 1)  # the first review went out, then the run stopped before the second


class CapAndActivationTests(unittest.TestCase):
    def test_at_most_three_reviews_are_sent_per_run_and_the_rest_wait_for_the_next(self) -> None:
        tickets = [make_ticket(i, frozen=FROZEN + timedelta(minutes=i)) for i in range(1, 6)]
        store = FakeReviewStore(tickets)
        first, sent1, _ = run(store)
        self.assertEqual((first["sent"], first["left_for_next_run"]), (3, 2))
        self.assertEqual(len(store.appended), 3)
        second, sent2, _ = run(store)
        self.assertEqual((second["sent"], second["left_for_next_run"]), (2, 0))
        self.assertEqual(rv.MAX_REVIEWS_PER_RUN, 3)

    def test_the_oldest_tickets_are_reviewed_first(self) -> None:
        tickets = [make_ticket(i, frozen=FROZEN + timedelta(minutes=10 - i)) for i in range(1, 6)]
        store = FakeReviewStore(tickets)
        run(store)
        self.assertEqual([a[0] for a in store.appended], [tid(5), tid(4), tid(3)])

    def test_a_dry_run_obeys_the_same_cap_and_sends_and_records_nothing(self) -> None:
        store = FakeReviewStore([make_ticket(i, frozen=FROZEN + timedelta(minutes=i)) for i in range(1, 6)])
        summary, sent, shown = run(store, dry=True)
        self.assertEqual((summary["reviewed"], summary["sent"], summary["left_for_next_run"]), (3, 0, 2))
        self.assertEqual((sent, store.appended), ([], []))
        self.assertEqual(len(shown), 3)

    def test_only_tickets_frozen_after_the_activation_time_are_reviewed(self) -> None:
        old = make_ticket(1, frozen=datetime(2029, 12, 31, 9, 0, tzinfo=UTC))
        new = make_ticket(2)
        store = FakeReviewStore([old, new])
        summary, sent, _ = run(store, after=datetime(2030, 1, 1, 0, 0, tzinfo=UTC))
        self.assertEqual(summary["sent"], 1)
        self.assertEqual([a[0] for a in store.appended], [tid(2)])
        self.assertEqual(store.asked_after, [datetime(2030, 1, 1, 0, 0, tzinfo=UTC)])

    def test_a_ticket_frozen_exactly_at_the_activation_time_is_not_reviewed(self) -> None:
        store = FakeReviewStore([make_ticket(1, frozen=ACTIVATION)])
        self.assertEqual(run(store)[0]["sent"], 0)

    def test_without_an_activation_time_nothing_is_reviewed_or_even_read(self) -> None:
        store = FakeReviewStore([make_ticket(1)])
        summary, sent, _ = run(store, after=None)
        self.assertEqual((summary["no_activation_time"], summary["sent"], sent, store.asked_after), (1, 0, [], []))

    def test_the_activation_setting_must_be_a_real_time(self) -> None:
        self.assertIsNone(rv._parse_frozen_after(""))
        self.assertIsNone(rv._parse_frozen_after(None))
        self.assertEqual(rv._parse_frozen_after("2030-01-01T00:00:00Z"), ACTIVATION)
        self.assertEqual(rv._parse_frozen_after("2030-01-01T03:00:00+03:00"), ACTIVATION)
        with self.assertRaises(rv.ReviewError):
            rv._parse_frozen_after("yesterday")


class ReportTests(unittest.TestCase):
    def report(self, results: str, upto: int | None = None, now: datetime = AFTER_DUE) -> str:
        t = make_ticket(1)
        store = FakeReviewStore([t])
        store.settle(t, results, upto)
        _, sent, _ = run(store, now)
        return "\n".join(sent)

    def test_a_complete_report_has_hits_per_arm_forced_and_reversed_picks_actuals_and_the_market_line(self) -> None:
        text = self.report("HHDA")  # A: 4 of 4; C draws the first (miss) so 3 of 4; B3 reverses the second to away (miss), 3 of 4
        self.assertIn("all 4 fixture(s) settled", text)
        self.assertIn("Hits per arm (settled picks): A 4/4 | B3 3/4 | C 3/4.", text)
        self.assertIn("- C forced draws: 0 of 1 hit", text)
        self.assertIn("- B3 reversed picks: 0 of 1 hit", text)
        self.assertIn("- Home 0 v Away 0: home 2-1 | A home hit | B3 home hit | C draw miss forced", text)
        self.assertIn("- Home 1 v Away 1: home 2-1 | A home hit | B3 away miss reversed | C home hit", text)
        self.assertIn("- Home 2 v Away 2: draw | A draw hit", text)
        self.assertIn("Model against market (3 settled fixture(s) with all three prices printed)", text)
        self.assertNotIn("Not settled yet", text)
        self.assertEqual(self.report("HHDA").count("Review of the ticket frozen 2030-01-01 09:00 UTC (12:00 EAT)"), 1)

    def test_the_market_line_uses_the_devigged_prices_and_the_models_probability_of_the_actual_result(self) -> None:
        text = self.report("HHDA")
        # model 50% home, 30% draw, 20% away; de-vigged 2.0 / 3.4 / 3.8 is about 47% / 28% / 25%
        market = rv.devig((2.0, 3.4, 3.8))
        expected = (0.5 + 0.5 + 0.3) / 3  # actual: home, home, draw on the three fixtures with prices
        self.assertIn(f"gave the actual result {expected * 100:.0f}% on average, the de-vigged market", text)
        self.assertIn(f"{(market[0] + market[0] + market[1]) / 3 * 100:.0f}%", text)

    def test_a_partial_report_says_what_is_unsettled_and_counts_only_settled_picks(self) -> None:
        text = self.report("HH", upto=2)
        self.assertIn("2 of 4 fixture(s) settled", text)
        self.assertIn("Hits per arm (settled picks): A 2/2 | B3 1/2 | C 1/2.", text)
        self.assertIn("Not settled yet (2 of 4):", text)
        self.assertIn("- Home 2 v Away 2 (kickoff 2030-01-01 12:00 UTC (15:00 EAT))", text)
        self.assertNotIn("- Home 2 v Away 2: draw", text)
        self.assertEqual(self._stored("HH", 2)[1:4], ("PARTIAL", 12, 6))

    def _stored(self, results, upto):
        t = make_ticket(1)
        store = FakeReviewStore([t])
        store.settle(t, results, upto)
        run(store)
        return store.appended[0]

    def test_the_complete_review_is_stored_as_complete_with_its_counts(self) -> None:
        stored = self._stored("HHDA", None)
        self.assertEqual(stored[1:4], ("COMPLETE", 12, 12))
        self.assertEqual(stored[4]["arms"]["A"], [4, 4])
        self.assertEqual(stored[4]["fixtures_settled"], 4)

    def test_every_report_states_when_its_results_were_read_in_both_clocks(self) -> None:
        for now in (AFTER_DUE, DUE, datetime(2030, 1, 4, 21, 30, tzinfo=UTC)):
            text = self.report("HHDA", now=now)
            self.assertIn(f"Results read {utc_and_eat(now)}. Results can still be corrected later.", text)
        self.assertEqual(utc_and_eat(datetime(2030, 1, 4, 21, 30, tzinfo=UTC)), "2030-01-04 21:30 UTC (00:30 EAT)")
        self.assertEqual(self._stored("HHDA", None)[4]["as_of"], AFTER_DUE.isoformat())

    def test_a_report_gives_no_stake_or_profit_advice(self) -> None:
        text = self.report("HHDA").lower()
        for word in ("stake", "profit", "bankroll", "you should bet", "guaranteed"):
            self.assertNotIn(word, text)
        self.assertIn("not betting advice", text)

    def test_a_long_report_is_split_at_line_breaks_never_cut(self) -> None:
        text = "\n".join(f"line {i} " + "x" * 60 for i in range(200))
        parts = rv.split_for_telegram(text, 1000)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        self.assertEqual("\n".join(parts), text)
        self.assertEqual(rv.split_for_telegram("short"), ["short"])

    def test_a_review_longer_than_one_telegram_message_is_sent_as_several_in_order(self) -> None:
        t = make_ticket(1, n=60)
        store = FakeReviewStore([t])
        store.settle(t, "H" * 60)
        _, sent, _ = run(store)
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(part) <= rv.MESSAGE_LIMIT for part in sent))
        joined = "\n".join(sent)
        self.assertIn("all 60 fixture(s) settled", sent[0])
        self.assertIn("- Home 59 v Away 59:", joined)  # the last fixture is not dropped
        self.assertTrue(joined.rstrip().endswith("says nothing about returns."))
        self.assertEqual(len(store.appended), 1)  # still one review, recorded once

    def test_the_dry_run_text_is_shown_only_when_asked_and_counts_are_all_main_prints(self) -> None:
        store = FakeReviewStore([make_ticket(1)])
        summary = rv.run_review(store, now=AFTER_DUE, frozen_after=ACTIVATION, send=None, dry_run=True, show=None)
        self.assertEqual((summary["reviewed"], summary["sent"], store.appended), (1, 0, []))


class MainTests(unittest.TestCase):
    ENV = {"SUPABASE_URL": "https://p.example", "SUPABASE_SERVICE_KEY": "k"}

    def test_a_real_run_without_telegram_settings_is_refused(self) -> None:
        self.assertEqual(rv.main([], env=self.ENV), 2)

    def test_missing_database_settings_are_refused(self) -> None:
        self.assertEqual(rv.main(["--dry-run"], env={}), 2)

    def test_a_bad_activation_time_is_refused(self) -> None:
        self.assertEqual(rv.main(["--dry-run", "--frozen-after", "soon"], env=self.ENV), 2)


# --- slate scoring -----------------------------------------------------------------------------------


def cand(fid: str, probs: tuple[float, float, float]) -> Candidate:
    return Candidate(fid, "L1", KICKOFF, f"p-{fid}", RATING_MODEL_NAME, *probs)


def srow(fid: str, odds=(None, None, None)) -> dict:
    return {"matched_fixture_id": fid, "home_odds": odds[0], "draw_odds": odds[1], "away_odds": odds[2]}


class SlateTests(unittest.TestCase):
    def test_a_fixture_on_two_uploads_counts_once_and_takes_the_first_complete_prices(self) -> None:
        rows = [srow("f1"), srow("f2", (2.0, 3.0, 4.0)), srow("f1", (1.5, 4.0, 6.0)), srow("f2", (9.0, 9.0, 9.0)), srow(None)]
        fixtures = sl.distinct_fixtures(rows)
        self.assertEqual([f.fixture_id for f in fixtures], ["f1", "f2"])
        self.assertEqual(fixtures[0].odds, (1.5, 4.0, 6.0))  # the first upload printed none; the next did
        self.assertEqual(fixtures[1].odds, (2.0, 3.0, 4.0))  # the earliest complete set is kept

    def test_a_partial_price_set_is_not_used(self) -> None:
        self.assertIsNone(sl.distinct_fixtures([srow("f1", (2.0, None, 4.0))])[0].odds)

    def test_scoring_counts_only_settled_fixtures_with_a_pre_kickoff_pick(self) -> None:
        fixtures = sl.distinct_fixtures([srow("f1"), srow("f2"), srow("f3"), srow("f4")])
        cands = {"f1": cand("f1", (0.6, 0.2, 0.2)), "f2": cand("f2", (0.2, 0.2, 0.6)), "f3": cand("f3", (0.5, 0.3, 0.2))}
        outcomes = {"f1": "HOME_WIN", "f2": "HOME_WIN", "f4": "DRAW"}  # f3 unsettled; f4 has no prediction
        score = sl.score_slates(fixtures, cands, outcomes)
        self.assertEqual((score.fixtures, score.with_prediction, score.settled, score.model_hits), (4, 3, 2, 1))
        self.assertAlmostEqual(score.model_probability_on_actual or 0, (0.6 + 0.2) / 2)

    def test_the_market_comparison_uses_only_settled_fixtures_with_all_three_prices(self) -> None:
        fixtures = sl.distinct_fixtures([srow("f1", (2.0, 3.4, 3.8)), srow("f2"), srow("f3", (1.5, 4.0, 7.0))])
        cands = {"f1": cand("f1", (0.3, 0.3, 0.4)), "f2": cand("f2", (0.6, 0.2, 0.2)), "f3": cand("f3", (0.5, 0.3, 0.2))}
        outcomes = {"f1": "HOME_WIN", "f2": "HOME_WIN", "f3": "AWAY_WIN"}
        score = sl.score_slates(fixtures, cands, outcomes)
        self.assertEqual((score.settled, score.compared), (3, 2))
        # f1: model picks away (0.4), market favourite is home; actual home. f3: model home, market home; actual away.
        self.assertEqual((score.compared_model_hits, score.compared_market_hits), (0, 1))
        m1, m3 = rv.devig((2.0, 3.4, 3.8)), rv.devig((1.5, 4.0, 7.0))
        self.assertAlmostEqual(score.compared_market_probability_on_actual or 0, (m1[0] + m3[2]) / 2)
        self.assertAlmostEqual(score.compared_model_probability_on_actual or 0, (0.3 + 0.2) / 2)

    def test_nothing_settled_gives_no_averages(self) -> None:
        score = sl.score_slates(sl.distinct_fixtures([srow("f1")]), {"f1": cand("f1", (0.4, 0.3, 0.3))}, {})
        self.assertEqual((score.settled, score.model_probability_on_actual, score.compared), (0, None, 0))
        self.assertIn("n/a", sl.format_score(score, AFTER_DUE))

    def test_the_slate_summary_prints_counts_only_no_names_and_no_advice(self) -> None:
        score = sl.score_slates(sl.distinct_fixtures([srow("f1", (2.0, 3.4, 3.8))]), {"f1": cand("f1", (0.5, 0.3, 0.2))}, {"f1": "HOME_WIN"})
        text = sl.format_score(score, AFTER_DUE)
        self.assertIn("1 distinct matched fixture(s), 1 settled", text)
        self.assertNotIn("f1", text)
        self.assertNotIn("stake", text.lower())

    def test_scoring_a_store_reads_the_rows_predictions_and_results_once_each(self) -> None:
        class Store:
            calls: list[str] = []

            def slate_reads(self):
                self.calls.append("rows")
                return [srow("f1"), srow("f1"), srow("f2")]

            def earliest_predictions_any_status(self, ids, model):
                self.calls.append(f"pred:{sorted(ids)}:{model}")
                return {"f1": cand("f1", (0.6, 0.2, 0.2))}

            def current_outcomes(self, ids):
                self.calls.append("results")
                return {"f1": "HOME_WIN"}

        store = Store()
        score = sl.score_stored_slates(store)
        self.assertEqual((score.fixtures, score.settled, score.model_hits), (2, 1, 1))
        self.assertEqual(store.calls, ["rows", f"pred:['f1', 'f2']:{RATING_MODEL_NAME}", "results"])

    def test_no_stored_slate_scores_to_zero(self) -> None:
        class Empty:
            def slate_reads(self):
                return []

        self.assertEqual(sl.score_stored_slates(Empty()).fixtures, 0)


class SlateNeverReachesTheScoreboardTests(unittest.TestCase):
    def test_a_slate_is_never_frozen_even_if_it_is_dispatched_by_mistake(self) -> None:
        class Store:
            appended: list = []

            def screenshot_ticket_exists(self, intake_id):
                return False

            def intake_ticket(self, intake_id):
                return {"id": intake_id, "kind": "SLATE"}

            def intake_rows(self, intake_id):
                raise AssertionError("a slate's rows must not even be read")

            def append_ticket(self, ticket):
                self.appended.append(ticket)

        store = Store()
        replies: list[str] = []
        summary = tm.run_ticket_mode(store, "11111111-2222-4333-8444-55555555abcd", AFTER_DUE, replies.append)
        self.assertEqual(summary, {"ticket_created": 0, "ticket_exists": 0, "ticket_skipped": 0, "slate_ignored": 1})
        self.assertEqual((store.appended, replies), ([], []))


# --- the ticket's times -------------------------------------------------------------------------------

COUNTS = DrawCounts(by_competition={"L1": (100, 40)}, total=1000, draws=250)


def shot_ticket(kickoffs):
    rows, cands = [], {}
    for i, k in enumerate(kickoffs):
        rows.append(tm.ShotRow(i + 1, f"Home {i}", f"Away {i}", k, (2.0, 3.4, 3.8), f"fx-{i}", "League"))
        cands[f"fx-{i}"] = Candidate(f"fx-{i}", "L1", k, f"pred-{i}", RATING_MODEL_NAME, 0.5, 0.3, 0.2, "League")
    return tm.build_shot_ticket(
        intake_id="11111111-2222-4333-8444-55555555abcd", ticket_date=FROZEN.date(), rows=rows, candidates=cands,
        counts=COUNTS, rules=[], frozen_at=FROZEN,
    )


class TicketTimesTests(unittest.TestCase):
    KICKS = [datetime(2030, 1, 1, 12, 0, tzinfo=UTC), datetime(2030, 1, 1, 18, 30, tzinfo=UTC), datetime(2030, 1, 1, 15, 0, tzinfo=UTC)]

    def test_a_ticket_stores_its_last_kickoff_and_a_review_due_36_hours_later(self) -> None:
        result = shot_ticket(self.KICKS)
        assert result is not None
        self.assertEqual(result.ticket.last_kickoff_at, datetime(2030, 1, 1, 18, 30, tzinfo=UTC))
        self.assertEqual(result.ticket.review_due_at, datetime(2030, 1, 3, 6, 30, tzinfo=UTC))

    def test_the_freeze_reply_shows_both_times_in_utc_and_eat(self) -> None:
        result = shot_ticket(self.KICKS)
        assert result is not None
        reply = tm.format_reply(result)
        self.assertIn("Last kickoff: 2030-01-01 18:30 UTC (21:30 EAT).", reply)
        self.assertIn("Review due: 2030-01-03 06:30 UTC (09:30 EAT), or sooner once every fixture has a result.", reply)

    def test_the_times_are_stored_with_the_ticket_and_only_when_set(self) -> None:
        result = shot_ticket(self.KICKS)
        assert result is not None
        for ticket, expect in ((result.ticket, True), (_without_times(result.ticket), False)):
            sent = []

            def opener(request, timeout, sent=sent):
                sent.append(request)
                return _Response()

            with patch("proracle.supabase_adapter.urlopen", opener):
                SupabaseRESTStore("https://p.example", "k").append_ticket(ticket)
            head = json.loads(sent[0].data)
            self.assertEqual("last_kickoff_at" in head and "review_due_at" in head, expect)
        self.assertEqual(head.get("last_kickoff_at"), None)

    def test_an_eat_time_is_three_hours_ahead_with_no_daylight_saving(self) -> None:
        self.assertEqual(utc_and_eat(datetime(2030, 7, 1, 22, 0, tzinfo=UTC)), "2030-07-01 22:00 UTC (01:00 EAT)")


def _without_times(ticket):
    from dataclasses import replace

    return replace(ticket, last_kickoff_at=None, review_due_at=None)


class _Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return None

    def read(self):
        return b""


# --- the adapter reads the review and the slate scoring use -------------------------------------------


class Recorder:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request, timeout):
        self.requests.append(request)
        return self.responses.pop(0) if self.responses else _Response()


class Page(_Response):
    def __init__(self, rows):
        self.body = json.dumps(rows).encode()
        self.headers = {"Content-Type": "application/json", "Content-Range": f"0-{max(len(rows) - 1, 0)}/{len(rows)}"}

    def read(self):
        return self.body


def store() -> SupabaseRESTStore:
    return SupabaseRESTStore("https://project.example", "test-service-secret")


class AdapterTests(unittest.TestCase):
    def test_reviewable_tickets_are_screenshot_tickets_frozen_after_the_activation_time(self) -> None:
        rec = Recorder(Page([{"id": "t1"}]))
        with patch("proracle.supabase_adapter.urlopen", rec):
            rows = store().reviewable_tickets(ACTIVATION)
        self.assertEqual(rows, [{"id": "t1"}])
        url = rec.requests[0].full_url
        self.assertIn("/rest/v1/tickets?", url)
        self.assertIn("ticket_kind=eq.SCREENSHOT", url)
        self.assertIn("frozen_at=gt.2030-01-01T00%3A00%3A00Z", url)
        self.assertIn("order=frozen_at.asc%2Cid.asc", url)

    def test_a_review_is_one_row_and_a_second_for_the_same_ticket_is_the_databases_to_refuse(self) -> None:
        rec = Recorder()
        with patch("proracle.supabase_adapter.urlopen", rec):
            store().append_review(tid(1), "PARTIAL", 12, 6, {"as_of": "x"})
        request = rec.requests[0]
        self.assertEqual((request.method, request.full_url.split("/rest/v1/")[1]), ("POST", "ticket_reviews"))
        self.assertEqual(
            json.loads(request.data),
            {"ticket_id": tid(1), "completeness": "PARTIAL", "picks_total": 12, "picks_settled": 6, "summary": {"as_of": "x"}},
        )

    def test_scoring_after_the_match_still_finds_the_pre_kickoff_prediction_though_the_fixture_is_no_longer_scheduled(self) -> None:
        def row(status):
            return {
                "id": "p1", "fixture_id": "f1", "predicted_at": "2030-01-01T06:00:00Z", "kickoff_at": "2030-01-01T12:00:00Z",
                "model_name": RATING_MODEL_NAME, "home_win_probability": 0.5, "draw_probability": 0.3,
                "away_win_probability": 0.2, "fixture": {"competition_id": "L1", "competition_name": "X", "status": status},
            }

        with patch("proracle.supabase_adapter.urlopen", Recorder(Page([row("COMPLETED")]))):
            self.assertEqual(store().earliest_predictions(["f1"], RATING_MODEL_NAME), {})  # freezing wants scheduled ones
        with patch("proracle.supabase_adapter.urlopen", Recorder(Page([row("COMPLETED")]))):
            got = store().earliest_predictions_any_status(["f1"], RATING_MODEL_NAME)
        self.assertEqual(got["f1"].prediction_id, "p1")
        late = row("COMPLETED")
        late["predicted_at"] = "2030-01-01T13:00:00Z"  # made after kickoff: never usable
        with patch("proracle.supabase_adapter.urlopen", Recorder(Page([late]))):
            self.assertEqual(store().earliest_predictions_any_status(["f1"], RATING_MODEL_NAME), {})

    def test_current_outcomes_resolve_corrections_and_drop_voided_results(self) -> None:
        def result(rid, fid, status, outcome, supersedes=None, at="2030-01-01T15:00:00Z"):
            return {
                "id": rid, "fixture_id": fid, "supersedes_result_id": supersedes, "status": status,
                "actual_outcome": outcome, "observed_at": at, "source_retrieved_at": at,
                "fixture": {"kickoff_at": "2030-01-01T12:00:00Z"},
            }

        rows = [
            result("r1", "f1", "FINAL", "HOME_WIN"),
            result("r2", "f1", "FINAL", "DRAW", supersedes="r1", at="2030-01-01T18:00:00Z"),  # corrected
            result("r3", "f2", "FINAL", "AWAY_WIN"),
            result("r4", "f2", "VOID", None, supersedes="r3", at="2030-01-01T19:00:00Z"),  # voided later
            result("r5", "f3", "FINAL", "HOME_WIN"),
        ]
        rec = Recorder(Page(rows))
        with patch("proracle.supabase_adapter.urlopen", rec):
            outcomes = store().current_outcomes(["f1", "f2", "f3", "f4"])
        self.assertEqual(outcomes, {"f1": "DRAW", "f3": "HOME_WIN"})
        self.assertIn("fixture_id=in.", rec.requests[0].full_url)

    def test_ids_are_read_in_chunks_of_forty(self) -> None:
        ids = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(85)]
        rec = Recorder(Page([]), Page([]), Page([]))
        with patch("proracle.supabase_adapter.urlopen", rec):
            store().stored_scores(ids)
        self.assertEqual(len(rec.requests), 3)

    def test_slate_reads_join_upload_times_to_matched_rows_without_an_embedded_join(self) -> None:
        rec = Recorder(
            Page([{"id": "s1", "received_at": "2030-01-01T10:00:00Z"}]),
            Page([{"intake_id": "s1", "position": 1, "matched_fixture_id": "f1", "home_odds": 2.0, "draw_odds": 3.0, "away_odds": 4.0}]),
        )
        with patch("proracle.supabase_adapter.urlopen", rec):
            rows = store().slate_reads()
        self.assertEqual(rows[0]["received_at"], "2030-01-01T10:00:00Z")
        self.assertIn("kind=eq.SLATE", rec.requests[0].full_url)
        self.assertIn("read_kind=eq.SLATE", rec.requests[1].full_url)
        self.assertIn("matched_fixture_id=not.is.null", rec.requests[1].full_url)

    def test_with_no_slates_the_rows_are_not_even_asked_for(self) -> None:
        rec = Recorder(Page([]))
        with patch("proracle.supabase_adapter.urlopen", rec):
            self.assertEqual(store().slate_reads(), [])
        self.assertEqual(len(rec.requests), 1)

    def test_the_review_reads_use_only_tables_the_runtime_role_may_read_and_one_it_may_insert(self) -> None:
        rec = Recorder(Page([]), Page([]), Page([]), Page([]), Page([]))
        with patch("proracle.supabase_adapter.urlopen", rec):
            s = store()
            s.reviewable_tickets(ACTIVATION)
            s.reviewed_ticket_ids()
            s.ticket_review_inputs(tid(1))
        tables = {r.full_url.split("/rest/v1/")[1].split("?")[0] for r in rec.requests}
        self.assertEqual(tables, {"tickets", "ticket_reviews", "ticket_fixtures", "ticket_arm_picks"})
        self.assertTrue(all(r.method == "GET" for r in rec.requests))


class MigrationAgreementTests(unittest.TestCase):
    def test_the_columns_and_the_table_the_code_writes_exist_in_the_applied_migration(self) -> None:
        from pathlib import Path

        sql = (Path(__file__).resolve().parents[1] / "supabase" / "migrations" / "20261129000000_proracle_ticket_slate.sql").read_text(encoding="utf-8")
        for needle in (
            "add column if not exists last_kickoff_at", "add column if not exists review_due_at",
            "create table if not exists proracle.ticket_reviews", "ticket_id uuid not null unique",
            "grant select, insert on table proracle.ticket_reviews to proracle_runtime",
        ):
            self.assertIn(needle, sql)


if __name__ == "__main__":
    unittest.main()
