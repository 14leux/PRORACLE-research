"""Fake-boundary tests for the provider/store-independent offline flow."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from uuid import UUID

from proracle.flow import run_offline_flow
from proracle.evaluate import Result


class FakeFixtures:
    def __init__(self, rows):
        self.rows = rows

    def upcoming_fixtures(self):
        return iter(self.rows)


class FakeFeatures:
    def __init__(self, rows):
        self.rows = rows

    def features_for(self, fixture_id):
        return self.rows[str(fixture_id)]


class FakeResults:
    def __init__(self, rows):
        self.rows = rows

    def final_result_for(self, fixture_id):
        return self.rows.get(str(fixture_id))


class FakeStore:
    def __init__(self):
        self.rows = []

    def append_prediction(self, record):
        self.rows.append(record)


class FixedClock:
    def now_for(self, fixture):
        return datetime(2026, 2, 1, 12, tzinfo=timezone.utc)


class FixedIds:
    def for_fixture(self, fixture):
        return UUID("00000000-0000-4000-8000-000000000401")


class OfflineFlowTests(unittest.TestCase):
    def fixture(self, fixture_id):
        return {
            "fixture_id": fixture_id,
            "home_team": "Synthetic Home",
            "away_team": "Synthetic Away",
            "kickoff_at": "2026-02-01T15:00:00Z",
        }

    def test_appends_predictions_and_exports_only_final_result_rows(self):
        first = "00000000-0000-4000-8000-000000000201"
        second = "00000000-0000-4000-8000-000000000202"
        training = [
            Result(
                "train-home",
                datetime(2026, 1, 1, 15, tzinfo=timezone.utc),
                "HOME_WIN",
                available_at=datetime(2026, 1, 1, 18, tzinfo=timezone.utc),
            )
        ]
        store = FakeStore()
        predictions, rows = run_offline_flow(
            FakeFixtures([self.fixture(first), self.fixture(second)]),
            FakeFeatures({first: {"home_form_index": 0.7}, second: {"home_form_index": 0.4}}),
            FakeResults(
                {
                    first: {
                        "fixture_id": first,
                        "status": "FINAL",
                        "actual_outcome": "HOME_WIN",
                        "finalized_at": "2026-02-01T17:00:00Z",
                    },
                    second: {
                        "fixture_id": second,
                        "status": "SCHEDULED",
                        "actual_outcome": None,
                        "finalized_at": None,
                    },
                }
            ),
            store,
            training,
            FixedClock(),
            FixedIds(),
        )
        self.assertEqual(len(predictions), 2)
        self.assertEqual(store.rows, predictions)
        self.assertEqual([row.fixture_id for row in rows], [UUID(first)])
        self.assertEqual(rows[0].as_dict()["actual_outcome"], "HOME_WIN")

    def test_rejects_duplicate_fixture_ids_before_saving_second_prediction(self):
        fixture = self.fixture("00000000-0000-4000-8000-000000000201")
        store = FakeStore()
        with self.assertRaisesRegex(ValueError, "duplicate fixture_id"):
            run_offline_flow(
                FakeFixtures([fixture, fixture]),
                FakeFeatures({fixture["fixture_id"]: {"home_form_index": 0.5}}),
                FakeResults({}),
                store,
                [Result("prior", datetime(2026, 1, 1, tzinfo=timezone.utc), "DRAW")],
                FixedClock(),
                FixedIds(),
            )
        self.assertEqual(len(store.rows), 1)


if __name__ == "__main__":
    unittest.main()
