"""Scoring of stored slates (docs/TICKETS.md, "Slates").

A slate is a large matched list kept for testing models. It freezes nothing, creates no ticket and never
reaches the arm scoreboard. After its fixtures settle it is scored in two ways, with each fixture counted
once however many uploads contain it:

- the rating model's earliest pre-kickoff pick against the real result;
- on the fixtures where all three prices were printed, the model against the de-vigged market (the
  favourite, and the probability each gave the result that happened).

Pure functions over stored values; the store reads live in ``supabase_adapter``. No stakes, no profit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from proracle.rating import RATING_MODEL_NAME
from proracle.ticket import Candidate, devig, pick_outcome

_OUTCOMES = ("HOME_WIN", "DRAW", "AWAY_WIN")


@dataclass(frozen=True)
class SlateFixture:
    """One distinct fixture on any slate: the earliest upload that printed all three prices, else the earliest."""

    fixture_id: str
    odds: tuple[float, float, float] | None


@dataclass(frozen=True)
class SlateScore:
    fixtures: int  # distinct matched fixtures across all uploads
    with_prediction: int
    settled: int  # with a prediction and a current FINAL result
    model_hits: int
    model_probability_on_actual: float | None  # mean over settled
    compared: int  # settled with all three prices printed
    compared_model_hits: int
    compared_market_hits: int
    compared_model_probability_on_actual: float | None
    compared_market_probability_on_actual: float | None


def distinct_fixtures(rows: Sequence[Mapping[str, Any]]) -> list[SlateFixture]:
    """Each matched fixture once. Rows are in upload order (oldest first); a fixture seen again keeps its place,
    but takes the first complete set of prices if its earlier rows had none."""
    order: list[str] = []
    odds: dict[str, tuple[float, float, float] | None] = {}
    for row in rows:
        fixture_id = row.get("matched_fixture_id")
        if not fixture_id:
            continue
        fixture_id = str(fixture_id)
        values = (row.get("home_odds"), row.get("draw_odds"), row.get("away_odds"))
        complete = (float(values[0]), float(values[1]), float(values[2])) if all(v is not None for v in values) else None
        if fixture_id not in odds:
            order.append(fixture_id)
            odds[fixture_id] = complete
        elif odds[fixture_id] is None and complete is not None:
            odds[fixture_id] = complete
    return [SlateFixture(i, odds[i]) for i in order]


def _market_pick(probabilities: tuple[float, float, float]) -> str:
    best = max(range(3), key=lambda i: (probabilities[i], -i))  # a tie goes to the earlier outcome
    return _OUTCOMES[best]


def score_slates(
    fixtures: Sequence[SlateFixture], candidates: Mapping[str, Candidate], outcomes: Mapping[str, str]
) -> SlateScore:
    with_prediction = settled = model_hits = 0
    model_on_actual: list[float] = []
    compared = compared_model_hits = compared_market_hits = 0
    cmp_model: list[float] = []
    cmp_market: list[float] = []
    for fixture in fixtures:
        cand = candidates.get(fixture.fixture_id)
        if cand is None:
            continue
        with_prediction += 1
        actual = outcomes.get(fixture.fixture_id)
        if actual is None:
            continue
        settled += 1
        probs = (cand.home, cand.draw, cand.away)
        index = _OUTCOMES.index(actual)
        model_hits += pick_outcome(*probs) == actual
        model_on_actual.append(probs[index])
        if fixture.odds is not None:
            market = devig(fixture.odds)
            compared += 1
            compared_model_hits += pick_outcome(*probs) == actual
            compared_market_hits += _market_pick(market) == actual
            cmp_model.append(probs[index])
            cmp_market.append(market[index])

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    return SlateScore(
        fixtures=len(fixtures),
        with_prediction=with_prediction,
        settled=settled,
        model_hits=model_hits,
        model_probability_on_actual=mean(model_on_actual),
        compared=compared,
        compared_model_hits=compared_model_hits,
        compared_market_hits=compared_market_hits,
        compared_model_probability_on_actual=mean(cmp_model),
        compared_market_probability_on_actual=mean(cmp_market),
    )


def score_stored_slates(store: Any) -> SlateScore:
    """Score every stored slate now: read the matched rows, the pre-kickoff predictions and the results."""
    fixtures = distinct_fixtures(store.slate_reads())
    ids = [f.fixture_id for f in fixtures]
    if not ids:
        return score_slates([], {}, {})
    return score_slates(
        fixtures,
        store.earliest_predictions_any_status(ids, RATING_MODEL_NAME),
        store.current_outcomes(ids),
    )


def format_score(score: SlateScore, as_of: datetime) -> str:
    """Plain text with counts only (safe for a log): no names, no stakes."""
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.0f}%"

    return "\n".join(
        [
            f"Slates, read {as_of:%Y-%m-%d %H:%M} UTC: {score.fixtures} distinct matched fixture(s), "
            f"{score.settled} settled with a pre-kickoff model pick.",
            f"Model pick hit {score.model_hits} of {score.settled}; it gave the real result {pct(score.model_probability_on_actual)} on average.",
            f"Against the market ({score.compared} settled with all three prices): model hit {score.compared_model_hits}, "
            f"market favourite hit {score.compared_market_hits}; probability given to the real result "
            f"{pct(score.compared_model_probability_on_actual)} (model) against {pct(score.compared_market_probability_on_actual)} (de-vigged market).",
            "Calibration tracking only, not betting advice.",
        ]
    )


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    import argparse
    import os
    import sys
    from datetime import timezone

    argparse.ArgumentParser(description="Score stored slates (prints counts only; sends nothing, writes nothing).").parse_args(argv)
    env = os.environ if env is None else env
    from proracle.supabase_adapter import SupabaseRESTStore, SupabaseStoreError

    url, key = env.get("SUPABASE_URL", ""), env.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("PRORACLE slate scoring: SUPABASE_URL and SUPABASE_SERVICE_KEY are required", file=sys.stderr)
        return 2
    try:
        store = SupabaseRESTStore(
            url, key, schema=env.get("PRORACLE_DB_SCHEMA", "proracle"), api_key=env.get("SUPABASE_API_KEY") or None
        )
        print(format_score(score_stored_slates(store), datetime.now(timezone.utc)))
    except SupabaseStoreError as error:
        print(f"PRORACLE slate scoring stopped: {error.error_code}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
