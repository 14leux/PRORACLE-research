"""Offline tests for the walk-forward Elo-Davidson rating model (all data fabricated)."""

from __future__ import annotations

import math
import random
import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID

from proracle.contracts import PROBABILITY_SUM_TOLERANCE
from proracle.domain import normalize_fixture
from proracle.rating import (
    FROZEN_CONSTANTS,
    HOME_ADVANTAGE,
    IDENTITY_CALIBRATION,
    INITIAL_RATING,
    K_FACTOR,
    RATING_INPUT_SCHEMA_VERSION,
    RATING_MODEL_NAME,
    RECAL_MIN_RESULTS,
    SHRINKAGE_GAMES,
    RatingResult,
    build_rating_state,
    create_rating_prediction,
    davidson_probabilities,
    fit_calibration,
    normalize_team_name,
    rating_probabilities,
    visible_results,
)

BASE = datetime(2030, 1, 1, 15, tzinfo=timezone.utc)
OUTCOMES = ("HOME_WIN", "DRAW", "AWAY_WIN")


def fid(number: int) -> str:
    return str(UUID(f"00000000-0000-4000-8000-{number:012d}"))


def result(
    number: int,
    home: str,
    away: str,
    outcome: str,
    *,
    kickoff: datetime,
    lag_hours: float = 2.0,
) -> RatingResult:
    return RatingResult(
        fixture_id=fid(number),
        home_team=home,
        away_team=away,
        kickoff_at=kickoff,
        available_at=kickoff + timedelta(hours=lag_hours),
        actual_outcome=outcome,
    )


def synthetic_league(seed: int = 7, teams: int = 12, rounds: int = 4) -> list[RatingResult]:
    """A fabricated double round-robin league drawn from fixed team strengths.

    Strengths are spread wide enough that a rating should beat the base rate; the numbers mean
    nothing about real football.
    """
    rng = random.Random(seed)  # noqa: S311  # nosec B311 -- seeded synthetic data
    names = [f"Synthetic Club {index:02d}" for index in range(teams)]
    strength = {name: rng.gauss(0.0, 180.0) for name in names}
    results: list[RatingResult] = []
    day = 0
    number = 1
    for _ in range(rounds):
        pairs = [(h, a) for h in names for a in names if h != a]
        rng.shuffle(pairs)
        for index, (home, away) in enumerate(pairs):
            if index % 6 == 0:
                day += 1
            kickoff = BASE + timedelta(days=day, hours=index % 6)
            probs = davidson_probabilities(strength[home], strength[away], 0.26)
            draw = rng.random()
            if draw < probs["HOME_WIN"]:
                outcome = "HOME_WIN"
            elif draw < probs["HOME_WIN"] + probs["DRAW"]:
                outcome = "DRAW"
            else:
                outcome = "AWAY_WIN"
            results.append(result(number, home, away, outcome, kickoff=kickoff))
            number += 1
    return results


class NormalizationTests(unittest.TestCase):
    def test_casefold_punctuation_and_whitespace(self) -> None:
        self.assertEqual(normalize_team_name("  F.C.  Synthetic-Town "), "fc synthetictown")
        self.assertEqual(normalize_team_name("SYNTHETIC  united"), "synthetic united")
        self.assertEqual(normalize_team_name("St. Example"), normalize_team_name("st example"))

    def test_empty_or_non_string_names_are_rejected(self) -> None:
        for bad in ("", " . ", None, 3):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_team_name(bad)  # type: ignore[arg-type]


class DavidsonTests(unittest.TestCase):
    def test_equal_strength_after_home_advantage_draws_at_the_draw_rate(self) -> None:
        probs = davidson_probabilities(INITIAL_RATING - HOME_ADVANTAGE, INITIAL_RATING, 0.27)
        self.assertAlmostEqual(probs["DRAW"], 0.27, places=12)
        self.assertAlmostEqual(probs["HOME_WIN"], probs["AWAY_WIN"], places=12)

    def test_home_advantage_favours_home_between_equal_teams(self) -> None:
        probs = davidson_probabilities(1500.0, 1500.0, 0.25)
        self.assertGreater(probs["HOME_WIN"], probs["AWAY_WIN"])
        self.assertAlmostEqual(math.fsum(probs.values()), 1.0, places=12)

    def test_extreme_ratings_stay_finite(self) -> None:
        probs = davidson_probabilities(9000.0, -9000.0, 0.25)
        self.assertTrue(all(math.isfinite(v) for v in probs.values()))

    def test_frozen_constants_match_the_documented_values(self) -> None:
        self.assertEqual((K_FACTOR, HOME_ADVANTAGE, INITIAL_RATING), (20.0, 70.0, 1500.0))
        self.assertEqual(SHRINKAGE_GAMES, 5.0)
        self.assertEqual(RECAL_MIN_RESULTS, 300)
        self.assertEqual(FROZEN_CONSTANTS["model"], "proracle/elo_davidson_recalibrated@1")


class WalkForwardTests(unittest.TestCase):
    def test_visible_results_require_availability_and_kickoff_strictly_before(self) -> None:
        as_of = BASE + timedelta(days=10)
        on_time = result(1, "A", "B", "HOME_WIN", kickoff=as_of - timedelta(hours=5))
        same_instant = result(2, "A", "C", "DRAW", kickoff=as_of - timedelta(hours=2))
        late = result(3, "B", "C", "AWAY_WIN", kickoff=as_of - timedelta(hours=1))
        visible = visible_results([late, same_instant, on_time], as_of)
        self.assertEqual([item.fixture_id for item in visible], [fid(1)])

    def test_result_available_before_kickoff_is_rejected(self) -> None:
        bad = RatingResult(fid(1), "A", "B", BASE, BASE - timedelta(minutes=1), "DRAW")
        with self.assertRaises(ValueError):
            visible_results([bad], BASE + timedelta(days=1))

    def test_no_history_is_an_error_not_a_guess(self) -> None:
        with self.assertRaises(ValueError):
            build_rating_state([], BASE)

    def test_first_ratings_follow_the_frozen_elo_update(self) -> None:
        state = build_rating_state(
            [result(1, "Home Side", "Away Side", "HOME_WIN", kickoff=BASE)], BASE + timedelta(days=1)
        )
        expected = 1.0 / (1.0 + 10.0 ** (-HOME_ADVANTAGE / 400.0))
        self.assertAlmostEqual(state.ratings["home side"], INITIAL_RATING + K_FACTOR * (1 - expected))
        self.assertAlmostEqual(state.ratings["away side"], INITIAL_RATING - K_FACTOR * (1 - expected))
        self.assertEqual(state.calibration, IDENTITY_CALIBRATION)

    def test_unknown_teams_get_exactly_the_base_rate(self) -> None:
        league = synthetic_league(rounds=1)
        state = build_rating_state(league, BASE + timedelta(days=400))
        probs = rating_probabilities(state, "Never Seen A", "Never Seen B")
        if state.calibration == IDENTITY_CALIBRATION:
            for outcome in OUTCOMES:
                self.assertAlmostEqual(probs[outcome], state.base_rate[outcome], places=12)

    def test_shrinkage_grows_with_history(self) -> None:
        early = [result(1, "Strong", "Weak", "HOME_WIN", kickoff=BASE)]
        later = early + [
            result(n, "Strong" if n % 2 else "Weak", "Weak" if n % 2 else "Strong",
                   "HOME_WIN" if n % 2 else "AWAY_WIN", kickoff=BASE + timedelta(days=n))
            for n in range(2, 12)
        ]
        p_early = rating_probabilities(build_rating_state(early, BASE + timedelta(days=30)), "Strong", "Weak")
        state_late = build_rating_state(later, BASE + timedelta(days=30))
        p_late = rating_probabilities(state_late, "Strong", "Weak")
        self.assertGreater(p_late["HOME_WIN"] - state_late.base_rate["HOME_WIN"], 0.0)
        self.assertGreater(p_late["HOME_WIN"], p_early["HOME_WIN"])

    def test_input_order_does_not_matter(self) -> None:
        league = synthetic_league(rounds=2)
        as_of = BASE + timedelta(days=200)
        shuffled = league[:]
        random.Random(3).shuffle(shuffled)  # noqa: S311  # nosec B311
        a, b = build_rating_state(league, as_of), build_rating_state(shuffled, as_of)
        self.assertEqual(a, b)
        names = league[0].home_team, league[0].away_team
        self.assertEqual(rating_probabilities(a, *names), rating_probabilities(b, *names))


class CalibrationTests(unittest.TestCase):
    def test_thin_data_falls_back_to_identity(self) -> None:
        samples = [({"HOME_WIN": 0.5, "DRAW": 0.3, "AWAY_WIN": 0.2}, "DRAW")] * (RECAL_MIN_RESULTS - 1)
        self.assertEqual(fit_calibration(samples), IDENTITY_CALIBRATION)

    def test_overconfident_inputs_are_flattened(self) -> None:
        rng = random.Random(11)  # noqa: S311  # nosec B311
        samples = []
        for _ in range(2000):
            # Stated 0.8 home, but outcomes really occur at 0.5 / 0.25 / 0.25.
            draw = rng.random()
            actual = "HOME_WIN" if draw < 0.5 else "DRAW" if draw < 0.75 else "AWAY_WIN"
            samples.append(({"HOME_WIN": 0.8, "DRAW": 0.1, "AWAY_WIN": 0.1}, actual))
        slope, draw_bias, away_bias = fit_calibration(samples)
        self.assertGreater(draw_bias, 0.0)
        self.assertGreater(away_bias, 0.0)
        from proracle.rating import _apply_calibration

        fitted = _apply_calibration({"HOME_WIN": 0.8, "DRAW": 0.1, "AWAY_WIN": 0.1}, (slope, draw_bias, away_bias))
        self.assertLess(fitted["HOME_WIN"], 0.7)

    def test_well_calibrated_inputs_stay_near_identity(self) -> None:
        rng = random.Random(5)  # noqa: S311  # nosec B311
        samples = []
        for _ in range(3000):
            h = rng.uniform(0.2, 0.6)
            d = rng.uniform(0.2, 0.3)
            p = {"HOME_WIN": h, "DRAW": d, "AWAY_WIN": 1 - h - d}
            draw = rng.random()
            actual = "HOME_WIN" if draw < h else "DRAW" if draw < h + d else "AWAY_WIN"
            samples.append((p, actual))
        slope, draw_bias, away_bias = fit_calibration(samples)
        self.assertLess(abs(slope - 1.0), 0.35)
        self.assertLess(abs(draw_bias), 0.35)
        self.assertLess(abs(away_bias), 0.35)


class SyntheticBacktestTests(unittest.TestCase):
    """Walk-forward backtest on a fabricated league: leakage, unit sums and a sanity edge."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.league = synthetic_league()
        cls.cutoff_index = len(cls.league) // 2

    def _predict(self, history, item, predicted_at):
        state = build_rating_state(history, predicted_at)
        return state, rating_probabilities(state, item.home_team, item.away_team)

    def test_every_prediction_sums_to_one_and_uses_only_earlier_results(self) -> None:
        model_loss = base_loss = 0.0
        scored = 0
        for item in self.league[self.cutoff_index :: 7]:
            predicted_at = item.kickoff_at - timedelta(hours=20)
            state, probs = self._predict(self.league, item, predicted_at)
            self.assertTrue(
                math.isclose(math.fsum(probs.values()), 1.0, abs_tol=PROBABILITY_SUM_TOLERANCE)
            )
            self.assertTrue(all(0.0 < value < 1.0 and math.isfinite(value) for value in probs.values()))
            expected_used = sum(
                1 for r in self.league if r.available_at < predicted_at and r.kickoff_at < predicted_at
            )
            self.assertEqual(state.results_used, expected_used)
            model_loss -= math.log(probs[item.actual_outcome])
            base_loss -= math.log(state.base_rate[item.actual_outcome])
            scored += 1
        self.assertGreater(scored, 30)
        # Fabricated strengths are wide, so the rating should beat the base rate here.
        self.assertLess(model_loss / scored, base_loss / scored)

    def test_a_result_available_after_prediction_time_cannot_change_the_prediction(self) -> None:
        item = self.league[self.cutoff_index]
        predicted_at = item.kickoff_at - timedelta(hours=20)
        _, before = self._predict(self.league, item, predicted_at)
        # A match that kicked off before prediction time but whose result arrived after it,
        # plus the target fixture's own result and every later one, flipped arbitrarily.
        late = RatingResult(
            fixture_id=fid(999_999),
            home_team=item.home_team,
            away_team=item.away_team,
            kickoff_at=predicted_at - timedelta(hours=3),
            available_at=predicted_at + timedelta(seconds=1),
            actual_outcome="AWAY_WIN",
        )
        tampered = [
            r if r.available_at < predicted_at else RatingResult(
                r.fixture_id, r.home_team, r.away_team, r.kickoff_at, r.available_at,
                OUTCOMES[(OUTCOMES.index(r.actual_outcome) + 1) % 3],
            )
            for r in self.league
        ] + [late]
        state_before = build_rating_state(self.league, predicted_at)
        state_after, after = self._predict(tampered, item, predicted_at)
        self.assertEqual(before, after)
        self.assertEqual(state_before.history_sha256, state_after.history_sha256)

    def test_result_available_at_the_prediction_instant_is_not_used(self) -> None:
        item = self.league[self.cutoff_index]
        predicted_at = item.kickoff_at - timedelta(hours=20)
        _, before = self._predict(self.league, item, predicted_at)
        boundary = RatingResult(
            fid(999_998), item.home_team, item.away_team,
            predicted_at - timedelta(hours=3), predicted_at, "AWAY_WIN",
        )
        _, after = self._predict([*self.league, boundary], item, predicted_at)
        self.assertEqual(before, after)

    def test_recalibration_is_active_once_history_is_long_enough(self) -> None:
        predicted_at = self.league[-1].kickoff_at + timedelta(days=1)
        state = build_rating_state(self.league, predicted_at)
        self.assertGreaterEqual(state.results_used, RECAL_MIN_RESULTS)
        self.assertNotEqual(state.calibration, IDENTITY_CALIBRATION)


class PredictionRecordTests(unittest.TestCase):
    def _fixture(self, kickoff: datetime):
        return normalize_fixture(
            {
                "fixture_id": fid(500),
                "home_team": "Synthetic Club 01",
                "away_team": "Synthetic Club 02",
                "kickoff_at": kickoff.isoformat().replace("+00:00", "Z"),
            }
        )

    def test_record_has_a_distinct_identity_and_valid_contract(self) -> None:
        league = synthetic_league(rounds=1)
        predicted_at = league[-1].kickoff_at + timedelta(days=1)
        state = build_rating_state(league, predicted_at)
        record = create_rating_prediction(
            self._fixture(predicted_at + timedelta(days=1)),
            state,
            prediction_id=UUID(fid(600)),
            predicted_at=predicted_at,
        )
        self.assertEqual(record.model.name, RATING_MODEL_NAME)
        self.assertEqual(record.model.provider, "proracle")
        self.assertEqual(record.input_schema_version, RATING_INPUT_SCHEMA_VERSION)
        self.assertNotEqual(record.model.name, "historical_frequency")
        self.assertRegex(record.input_snapshot_hash, r"^[0-9a-f]{64}$")
        again = create_rating_prediction(
            self._fixture(predicted_at + timedelta(days=1)),
            build_rating_state(league, predicted_at),
            prediction_id=UUID(fid(601)),
            predicted_at=predicted_at,
        )
        self.assertEqual(record.input_snapshot_hash, again.input_snapshot_hash)
        self.assertEqual(record.probabilities, again.probabilities)

    def test_state_must_match_prediction_time_and_be_prematch(self) -> None:
        league = synthetic_league(rounds=1)
        predicted_at = league[-1].kickoff_at + timedelta(days=1)
        state = build_rating_state(league, predicted_at)
        with self.assertRaises(ValueError):
            create_rating_prediction(
                self._fixture(predicted_at + timedelta(days=2)),
                state,
                prediction_id=UUID(fid(602)),
                predicted_at=predicted_at + timedelta(hours=1),
            )
        with self.assertRaises(ValueError):
            create_rating_prediction(
                self._fixture(predicted_at),
                state,
                prediction_id=UUID(fid(603)),
                predicted_at=predicted_at,
            )

    def test_labels_that_collide_after_normalization_are_rejected(self) -> None:
        state = build_rating_state(synthetic_league(rounds=1), BASE + timedelta(days=400))
        with self.assertRaises(ValueError):
            rating_probabilities(state, "Synthetic F.C.", "synthetic fc")


if __name__ == "__main__":
    unittest.main()
