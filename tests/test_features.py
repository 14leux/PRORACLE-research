"""Offline tests for the point-in-time results features (all data fabricated)."""

from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID

from proracle.features import (
    CONGESTION_DAYS,
    FEATURES_VERSION,
    FORM_WINDOW,
    H2H_WINDOW,
    FeatureHistory,
    competition_cup_keyword,
    compute_features,
    feature_names,
    features_for,
)
from proracle.rating import RatingResult

KICKOFF = datetime(2030, 3, 20, 15, tzinfo=timezone.utc)
AS_OF = datetime(2030, 3, 19, 9, tzinfo=timezone.utc)  # 30 hours before the fixture


def fid(number: int) -> str:
    return str(UUID(f"00000000-0000-4000-8000-{number:012d}"))


def res(number, home, away, outcome, days_before_kickoff, lag_hours=2.0):
    kickoff = KICKOFF - timedelta(days=days_before_kickoff)
    return RatingResult(
        fixture_id=fid(number),
        home_team=home,
        away_team=away,
        kickoff_at=kickoff,
        available_at=kickoff + timedelta(hours=lag_hours),
        actual_outcome=outcome,
    )


def vec(results, home="Alpha", away="Beta", competition=None, as_of=AS_OF, kickoff=KICKOFF):
    return features_for(results, as_of, home, away, kickoff, competition).as_dict()


class LeakageTests(unittest.TestCase):
    def test_results_available_at_or_after_as_of_are_invisible(self):
        late = RatingResult(
            fixture_id=fid(1),
            home_team="Alpha",
            away_team="Gamma",
            kickoff_at=AS_OF - timedelta(hours=3),
            available_at=AS_OF,  # available exactly at the prediction time: not yet visible
            actual_outcome="HOME_WIN",
        )
        self.assertEqual(vec([late])["home_games_visible"], 0)
        early = RatingResult(**{**late.__dict__, "available_at": AS_OF - timedelta(seconds=1)})
        self.assertEqual(vec([early])["home_games_visible"], 1)

    def test_a_result_that_kicks_off_after_as_of_is_invisible(self):
        future = res(1, "Alpha", "Gamma", "HOME_WIN", days_before_kickoff=0.2)
        self.assertGreater(future.kickoff_at, AS_OF)
        self.assertEqual(vec([future])["home_games_visible"], 0)

    def test_adding_later_results_never_changes_the_vector_or_digest(self):
        base = [res(1, "Alpha", "Gamma", "HOME_WIN", 10), res(2, "Delta", "Beta", "DRAW", 8)]
        later = [
            res(3, "Alpha", "Beta", "AWAY_WIN", 0.1),
            res(4, "Alpha", "Delta", "HOME_WIN", 0.05),
        ]
        a = features_for(base, AS_OF, "Alpha", "Beta", KICKOFF)
        b = features_for(base + later, AS_OF, "Alpha", "Beta", KICKOFF)
        self.assertEqual(a.entries, b.entries)
        self.assertEqual(a.sha256, b.sha256)

    def test_as_of_must_precede_kickoff(self):
        history = FeatureHistory([], AS_OF)
        with self.assertRaises(ValueError):
            compute_features(history, "Alpha", "Beta", AS_OF)
        with self.assertRaises(ValueError):
            compute_features(history, "Alpha", "Beta", AS_OF - timedelta(hours=1))

    def test_naive_datetimes_are_rejected(self):
        with self.assertRaises(ValueError):
            FeatureHistory([], datetime(2030, 3, 19, 9))
        with self.assertRaises(ValueError):
            compute_features(FeatureHistory([], AS_OF), "Alpha", "Beta", datetime(2030, 3, 20, 15))

    def test_same_team_is_rejected(self):
        with self.assertRaises(ValueError):
            vec([], home="Alpha FC", away="alpha  f.c")


class MissingIsNeverZeroTests(unittest.TestCase):
    def test_empty_history(self):
        values = vec([])
        self.assertEqual(values["home_games_visible"], 0)
        self.assertEqual(values["home_form_n"], 0)
        self.assertIsNone(values["home_rest_days"])
        self.assertIsNone(values["away_rest_days"])
        self.assertEqual(values["h2h_n"], 0)
        self.assertIsNone(values["competition_cup_keyword"])

    def test_rest_days_unknown_for_a_team_without_history_known_for_the_other(self):
        values = vec([res(1, "Alpha", "Gamma", "DRAW", 5)])
        self.assertAlmostEqual(values["home_rest_days"], 5.0)
        self.assertIsNone(values["away_rest_days"])


class FormTests(unittest.TestCase):
    def test_perspective_and_points(self):
        results = [
            res(1, "Alpha", "Gamma", "HOME_WIN", 20),  # Alpha W (home)
            res(2, "Gamma", "Alpha", "HOME_WIN", 15),  # Alpha L (away)
            res(3, "Delta", "Alpha", "AWAY_WIN", 10),  # Alpha W (away)
            res(4, "Alpha", "Delta", "DRAW", 5),  # Alpha D (home)
        ]
        values = vec(results)
        self.assertEqual(
            (
                values["home_form_n"],
                values["home_form_wins"],
                values["home_form_draws"],
                values["home_form_losses"],
                values["home_form_points"],
            ),
            (4, 2, 1, 1, 7),
        )

    def test_form_uses_only_the_most_recent_window(self):
        results = [res(i, "Alpha", f"T{i}", "AWAY_WIN", 40 - i) for i in range(1, 6)]  # 5 losses
        results += [res(10 + i, "Alpha", f"U{i}", "HOME_WIN", 9 - i) for i in range(FORM_WINDOW)]
        values = vec(results)
        self.assertEqual(values["home_form_n"], FORM_WINDOW)
        self.assertEqual(values["home_form_wins"], FORM_WINDOW)
        self.assertEqual(values["home_games_visible"], 10)

    def test_venue_form_only_counts_the_teams_own_venue(self):
        results = [
            res(1, "Alpha", "Gamma", "HOME_WIN", 12),  # Alpha at home
            res(2, "Gamma", "Alpha", "HOME_WIN", 9),  # Alpha away: not venue form for Alpha
            res(3, "Delta", "Beta", "AWAY_WIN", 7),  # Beta away win
            res(4, "Beta", "Delta", "HOME_WIN", 4),  # Beta at home: not venue form for Beta
        ]
        values = vec(results)
        self.assertEqual((values["home_venue_form_n"], values["home_venue_form_wins"]), (1, 1))
        self.assertEqual((values["away_venue_form_n"], values["away_venue_form_wins"]), (1, 1))
        self.assertEqual(values["home_form_n"], 2)
        self.assertEqual(values["away_form_n"], 2)


class RestAndCongestionTests(unittest.TestCase):
    def test_rest_days_are_measured_to_the_fixtures_kickoff_not_to_as_of(self):
        results = [res(1, "Alpha", "Gamma", "DRAW", 4.0)]
        early = vec(results, as_of=KICKOFF - timedelta(days=2))
        late = vec(results, as_of=KICKOFF - timedelta(hours=1))
        self.assertAlmostEqual(early["home_rest_days"], 4.0)
        self.assertAlmostEqual(late["home_rest_days"], 4.0)
        self.assertAlmostEqual(early["hours_to_kickoff"], 48.0)
        self.assertAlmostEqual(late["hours_to_kickoff"], 1.0)

    def test_congestion_windows_include_the_start_and_exclude_the_kickoff(self):
        results = [
            res(1, "Alpha", "G1", "DRAW", 7.0),  # exactly 7 days before: counted in 7d
            res(2, "Alpha", "G2", "DRAW", 7.01),  # just outside 7d, inside 14d
            res(3, "Alpha", "G3", "DRAW", 20),  # inside 30d only
            res(4, "Alpha", "G4", "DRAW", 31),  # outside every window
        ]
        values = vec(results)
        self.assertEqual(
            [values[f"home_matches_{days}d"] for days in CONGESTION_DAYS], [1, 2, 3]
        )


class HeadToHeadTests(unittest.TestCase):
    def test_h2h_is_from_the_fixtures_home_teams_point_of_view(self):
        results = [
            res(1, "Alpha", "Beta", "HOME_WIN", 30),  # Alpha beat Beta
            res(2, "Beta", "Alpha", "HOME_WIN", 20),  # Beta beat Alpha (Alpha was away)
            res(3, "Beta", "Alpha", "AWAY_WIN", 10),  # Alpha beat Beta (Alpha away)
            res(4, "Alpha", "Beta", "DRAW", 5),
            res(5, "Alpha", "Gamma", "HOME_WIN", 3),  # not a meeting
        ]
        values = vec(results)
        self.assertEqual(
            (values["h2h_n"], values["h2h_home_wins"], values["h2h_draws"], values["h2h_away_wins"]),
            (4, 2, 1, 1),
        )
        flipped = vec(results, home="Beta", away="Alpha")
        self.assertEqual((flipped["h2h_home_wins"], flipped["h2h_away_wins"]), (1, 2))

    def test_h2h_uses_only_the_most_recent_window(self):
        results = [res(i, "Alpha", "Beta", "AWAY_WIN", 100 - i) for i in range(1, 4)]
        results += [res(10 + i, "Alpha", "Beta", "HOME_WIN", 20 - i) for i in range(H2H_WINDOW)]
        values = vec(results)
        self.assertEqual((values["h2h_n"], values["h2h_home_wins"]), (H2H_WINDOW, H2H_WINDOW))


class CompetitionKeywordTests(unittest.TestCase):
    def test_keyword_hint(self):
        for name in ("FA Cup", "Copa del Rey", "Coppa Italia", "Coupe de France", "DFB-Pokal"):
            self.assertEqual(competition_cup_keyword(name), 1, name)
        for name in ("Taça de Portugal", "KNVB Beker", "Svenska Cupen"):
            self.assertEqual(competition_cup_keyword(name), 1, name)
        self.assertEqual(competition_cup_keyword("Premier League"), 0)
        self.assertIsNone(competition_cup_keyword(None))
        self.assertIsNone(competition_cup_keyword("   "))


class DeterminismTests(unittest.TestCase):
    def synthetic(self):
        rng = random.Random(11)
        teams = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
        out = []
        for number in range(1, 60):
            home, away = rng.sample(teams, 2)
            out.append(res(number, home, away, rng.choice(["HOME_WIN", "DRAW", "AWAY_WIN"]), 60 - number))
        return out

    def test_input_order_never_matters(self):
        results = self.synthetic()
        shuffled = results[:]
        random.Random(3).shuffle(shuffled)
        a = features_for(results, AS_OF, "Alpha", "Beta", KICKOFF, "FA Cup")
        b = features_for(shuffled, AS_OF, "Alpha", "Beta", KICKOFF, "FA Cup")
        self.assertEqual((a.entries, a.sha256), (b.entries, b.sha256))

    def test_name_normalization_matches_the_rating_model(self):
        results = [res(1, "Alpha F.C.", "Gamma", "HOME_WIN", 5)]
        self.assertEqual(vec(results, home="alpha  fc")["home_games_visible"], 1)

    def test_digest_changes_with_any_visible_result_and_carries_the_version(self):
        results = self.synthetic()
        first = results[0]  # a visible result: it kicked off weeks before AS_OF
        flipped = "DRAW" if first.actual_outcome != "DRAW" else "HOME_WIN"
        changed = [RatingResult(**{**first.__dict__, "actual_outcome": flipped})] + results[1:]
        a = features_for(results, AS_OF, "Alpha", "Beta", KICKOFF)
        b = features_for(changed, AS_OF, "Alpha", "Beta", KICKOFF)
        self.assertEqual(a.version, FEATURES_VERSION)
        self.assertNotEqual(a.sha256, b.sha256)

    def test_the_feature_set_is_fixed_and_json_safe(self):
        names = feature_names()
        self.assertEqual(names, tuple(sorted(names)))
        self.assertEqual(len(names), len(set(names)))
        values = vec(self.synthetic(), competition="Premier League")
        self.assertEqual(tuple(sorted(values)), names)
        for value in values.values():
            self.assertTrue(value is None or isinstance(value, (int, float)))

    def test_one_history_serves_many_fixtures(self):
        history = FeatureHistory(self.synthetic(), AS_OF)
        a = compute_features(history, "Alpha", "Beta", KICKOFF)
        b = compute_features(history, "Gamma", "Delta", KICKOFF + timedelta(hours=3))
        self.assertEqual(a.as_of, b.as_of)
        self.assertNotEqual(a.sha256, b.sha256)


if __name__ == "__main__":
    unittest.main()
