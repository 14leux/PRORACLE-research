"""Small transparent baselines shared by prediction and evaluation paths."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

OUTCOMES = ("HOME_WIN", "DRAW", "AWAY_WIN")


def add_one_smoothed_probabilities(outcomes: Iterable[str]) -> dict[str, float]:
    """Estimate fixed 1X2 class priors using Laplace (add-one) smoothing."""
    counts: Counter[str] = Counter()
    for outcome in outcomes:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome!r}")
        counts[outcome] += 1
    total = sum(counts.values())
    if total == 0:
        raise ValueError("at least one historical outcome is required")
    denominator = total + len(OUTCOMES)
    return {outcome: (counts[outcome] + 1) / denominator for outcome in OUTCOMES}
