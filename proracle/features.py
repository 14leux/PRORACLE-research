"""Point-in-time match features computed only from PRORACLE's own stored results (plan E0).

No provider field, odds, lineup, standing or news is used. The inputs are exactly what the
rating model already uses: fixture team labels, kickoff, local availability time and the
regulation-time final outcome (decision 0001). Because the store keeps outcomes and not scores,
there are no goal features.

Everything is pure and deterministic. For a prediction made at ``as_of`` for a fixture that
kicks off at ``kickoff_at``, only results whose availability time is strictly before ``as_of``
and whose kickoff is strictly before ``as_of`` are used (``proracle.rating.visible_results``),
and ``as_of`` must be strictly before ``kickoff_at``. Input order never matters. A feature that
cannot be computed is ``None``, never 0: counts carry an explicit ``*_n`` sample size, and a
rest-day value is ``None`` when the team has no visible result. See docs/FEATURES.md.

Changing any constant or feature below is a new feature version: bump ``FEATURES_VERSION`` in the
same change and never edit a released version in place.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from proracle.rating import RatingResult, normalize_team_name, visible_results

# --- Frozen constants: feature set v1 -----------------------------------------------------
FEATURES_VERSION = "1"
FEATURES_INPUT_SCHEMA_VERSION = "results-v1"
FORM_WINDOW = 5  # most recent visible results of a team (any venue) / of a team at its venue
H2H_WINDOW = 5  # most recent visible meetings of the two teams (either venue)
CONGESTION_DAYS = (7, 14, 30)  # visible results in [kickoff - days, kickoff)
POINTS = {"W": 3, "D": 1, "L": 0}
# Name keywords only; this is a hint, not a classification (see docs/FEATURES.md).
CUP_KEYWORDS = ("cup", "copa", "coppa", "coupe", "pokal", "taça", "taca", "beker", "cupen")
# ------------------------------------------------------------------------------------------

_TEAM_VIEW = {
    # outcome stored from the home side's view -> (home team's view, away team's view)
    "HOME_WIN": ("W", "L"),
    "DRAW": ("D", "D"),
    "AWAY_WIN": ("L", "W"),
}


@dataclass(frozen=True)
class FeatureVector:
    """One fixture's features at one prediction time, with a canonical digest."""

    version: str
    as_of: datetime
    kickoff_at: datetime
    home_key: str
    away_key: str
    entries: tuple[tuple[str, int | float | None], ...]
    sha256: str

    def as_dict(self) -> dict[str, int | float | None]:
        return dict(self.entries)


@dataclass(frozen=True)
class _Match:
    fixture_id: str
    kickoff_at: datetime
    home_key: str
    away_key: str
    outcome: str


class FeatureHistory:
    """The visible results at one ``as_of``, indexed by team. Build once per run and reuse for
    every fixture predicted at that time."""

    def __init__(self, results: Iterable[RatingResult], as_of: datetime) -> None:
        if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be a timezone-aware datetime")
        self.as_of = as_of.astimezone(timezone.utc)
        visible = visible_results(results, self.as_of)  # kickoff order, deterministic
        self.results_used = len(visible)
        self._by_team: dict[str, list[_Match]] = defaultdict(list)
        digest = hashlib.sha256()
        for item in visible:
            match = _Match(
                fixture_id=item.fixture_id,
                kickoff_at=item.kickoff_at.astimezone(timezone.utc),
                home_key=normalize_team_name(item.home_team),
                away_key=normalize_team_name(item.away_team),
                outcome=item.actual_outcome,
            )
            self._by_team[match.home_key].append(match)
            self._by_team[match.away_key].append(match)
            digest.update(
                f"{match.fixture_id}|{_stamp(match.kickoff_at)}|{match.home_key}|"
                f"{match.away_key}|{match.outcome}\n".encode()
            )
        self.history_sha256 = digest.hexdigest()

    def matches(self, team_key: str) -> list[_Match]:
        return self._by_team.get(team_key, [])


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _view(match: _Match, team_key: str) -> str:
    home_view, away_view = _TEAM_VIEW[match.outcome]
    return home_view if match.home_key == team_key else away_view


def _record(views: Sequence[str]) -> dict[str, int]:
    wins, draws, losses = (views.count("W"), views.count("D"), views.count("L"))
    return {
        "n": len(views),
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "points": wins * POINTS["W"] + draws * POINTS["D"] + losses * POINTS["L"],
    }


def _team_block(
    history: FeatureHistory, prefix: str, team_key: str, venue: str, kickoff_at: datetime
) -> dict[str, int | float | None]:
    """Form (any venue), venue form, rest days and congestion for one team."""
    matches = history.matches(team_key)  # kickoff order, all before as_of < kickoff_at
    values: dict[str, int | float | None] = {f"{prefix}_games_visible": len(matches)}
    recent = matches[-FORM_WINDOW:]
    for suffix, entry in _record([_view(m, team_key) for m in recent]).items():
        values[f"{prefix}_form_{suffix}"] = entry
    at_venue = [
        m
        for m in matches
        if (m.home_key == team_key if venue == "home" else m.away_key == team_key)
    ][-FORM_WINDOW:]
    for suffix, entry in _record([_view(m, team_key) for m in at_venue]).items():
        values[f"{prefix}_venue_form_{suffix}"] = entry
    values[f"{prefix}_rest_days"] = (
        (kickoff_at - matches[-1].kickoff_at) / timedelta(days=1) if matches else None
    )
    for days in CONGESTION_DAYS:
        start = kickoff_at - timedelta(days=days)
        values[f"{prefix}_matches_{days}d"] = sum(1 for m in matches if m.kickoff_at >= start)
    return values


def _h2h_block(
    history: FeatureHistory, home_key: str, away_key: str
) -> dict[str, int | float | None]:
    """Meetings of the two teams, from this fixture's home team's point of view."""
    meetings = [m for m in history.matches(home_key) if away_key in (m.home_key, m.away_key)]
    record = _record([_view(m, home_key) for m in meetings[-H2H_WINDOW:]])
    return {
        "h2h_n": record["n"],
        "h2h_home_wins": record["wins"],
        "h2h_draws": record["draws"],
        "h2h_away_wins": record["losses"],
    }


def competition_cup_keyword(competition_name: str | None) -> int | None:
    """1 if the competition name contains a cup keyword, 0 if it does not, None if unknown."""
    if competition_name is None or not str(competition_name).strip():
        return None
    folded = str(competition_name).casefold()
    return 1 if any(word in folded for word in CUP_KEYWORDS) else 0


def compute_features(
    history: FeatureHistory,
    home_team: str,
    away_team: str,
    kickoff_at: datetime,
    competition_name: str | None = None,
) -> FeatureVector:
    """The feature vector for one fixture at ``history.as_of``."""
    if (
        not isinstance(kickoff_at, datetime)
        or kickoff_at.tzinfo is None
        or kickoff_at.utcoffset() is None
    ):
        raise ValueError("kickoff_at must be a timezone-aware datetime")
    kickoff = kickoff_at.astimezone(timezone.utc)
    if not history.as_of < kickoff:
        raise ValueError("features are only defined for a prediction made before kickoff")
    home_key, away_key = normalize_team_name(home_team), normalize_team_name(away_team)
    if home_key == away_key:
        raise ValueError("home and away team must be different teams")

    values: dict[str, int | float | None] = {}
    values.update(_team_block(history, "home", home_key, "home", kickoff))
    values.update(_team_block(history, "away", away_key, "away", kickoff))
    values.update(_h2h_block(history, home_key, away_key))
    values["competition_cup_keyword"] = competition_cup_keyword(competition_name)
    values["hours_to_kickoff"] = (kickoff - history.as_of) / timedelta(hours=1)

    entries = tuple(sorted(values.items()))
    canonical = json.dumps(
        {
            "version": FEATURES_VERSION,
            "input_schema": FEATURES_INPUT_SCHEMA_VERSION,
            "history_sha256": history.history_sha256,
            "as_of": _stamp(history.as_of),
            "kickoff_at": _stamp(kickoff),
            "home": home_key,
            "away": away_key,
            "entries": entries,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return FeatureVector(
        version=FEATURES_VERSION,
        as_of=history.as_of,
        kickoff_at=kickoff,
        home_key=home_key,
        away_key=away_key,
        entries=entries,
        sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def features_for(
    results: Iterable[RatingResult],
    as_of: datetime,
    home_team: str,
    away_team: str,
    kickoff_at: datetime,
    competition_name: str | None = None,
) -> FeatureVector:
    """Convenience for one fixture. For many fixtures at one time, build ``FeatureHistory`` once."""
    return compute_features(
        FeatureHistory(results, as_of), home_team, away_team, kickoff_at, competition_name
    )


def feature_names() -> tuple[str, ...]:
    """The fixed, sorted feature names of this version (computed from an empty history)."""
    probe_as_of = datetime(2000, 1, 1, tzinfo=timezone.utc)
    vector = compute_features(
        FeatureHistory((), probe_as_of),
        "a",
        "b",
        probe_as_of + timedelta(days=1),
        None,
    )
    return tuple(name for name, _ in vector.entries)


__all__ = [
    "CONGESTION_DAYS",
    "CUP_KEYWORDS",
    "FEATURES_INPUT_SCHEMA_VERSION",
    "FEATURES_VERSION",
    "FORM_WINDOW",
    "H2H_WINDOW",
    "FeatureHistory",
    "FeatureVector",
    "competition_cup_keyword",
    "compute_features",
    "feature_names",
    "features_for",
]
