"""Daily simulated ticket: a seeded 20-fixture selection with arms A and C (see docs/TICKETS.md).

Nothing here changes a prediction. A ticket is a record of what the arms would have picked,
written before every kickoff on it and never altered, so the arms can be scored against results
later. Arm A is the live rating model's earliest pre-kickoff prediction for the fixture. Arm C
forces the FORCED_DRAWS fixtures with the highest draw score to DRAW and otherwise follows arm A.
Arm B is written only when a rule set is active, and none exists yet.

The selection and the arms are pure functions of the stored rows and the date, so a ticket can be
recomputed and checked. No odds, stake or provider payload is used or stored.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from proracle.rating import RATING_MODEL_NAME

TICKET_KIND = "SIMULATED"
DESIGN_VERSION = "sim-v1"
DRAW_ARM_VERSION = "draw-score-v1"
TICKET_SIZE = 20
FORCED_DRAWS = 5
# A day's pool must hold a full ticket, so tickets stay comparable from day to day.
MIN_POOL = TICKET_SIZE
POOL_WINDOW = timedelta(hours=24)
# Prior weight, in matches, pulling a competition's draw rate toward the global rate.
SHRINK_MATCHES = 40
_NAMESPACE = uuid.UUID("5f0c3d52-7a53-4c5b-9a45-2c7a0b1d9e11")

OUTCOMES = ("HOME_WIN", "DRAW", "AWAY_WIN")


@dataclass(frozen=True)
class Candidate:
    """A fixture that kicks off inside the pool window, with its earliest rating prediction."""

    fixture_id: str
    competition_id: str | None
    kickoff_at: datetime
    prediction_id: str
    model_name: str
    home: float
    draw: float
    away: float
    competition_name: str | None = None


@dataclass(frozen=True)
class DrawCounts:
    """Settled history: matches and draws per competition, and in total."""

    by_competition: Mapping[str, tuple[int, int]]
    total: int
    draws: int


@dataclass(frozen=True)
class ArmPick:
    fixture_id: str
    arm: str
    arm_version: str
    pick: str
    forced: bool
    score: float | None
    source_prediction_id: str | None
    kickoff_at: datetime
    # Arm B only: FORCED_DRAW or REVERSED (arm C marks its forced draws with `forced`).
    adjustment: str | None = None


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    ticket_date: date
    seed: str
    pool_size: int
    frozen_at: datetime
    fixtures: tuple[str, ...]
    picks: tuple[ArmPick, ...]
    design_version: str = DESIGN_VERSION
    ticket_kind: str = TICKET_KIND
    # Set for a screenshot ticket: the stored read it was computed from.
    intake_id: str | None = None


class TicketStore(Protocol):
    def ticket_exists(self, ticket_date: date, design_version: str) -> bool: ...

    def ticket_pool(
        self, after: datetime, until: datetime, model_name: str
    ) -> list[Candidate]: ...

    def draw_counts(self) -> DrawCounts: ...

    def append_ticket(self, ticket: Ticket) -> None: ...


def ticket_seed(ticket_date: date) -> str:
    return f"proracle-sim-ticket:{DESIGN_VERSION}:{ticket_date.isoformat()}"


def ticket_id_for(ticket_date: date) -> str:
    """Deterministic, so a retried insert collides instead of creating a second ticket."""
    return str(uuid.uuid5(_NAMESPACE, ticket_seed(ticket_date)))


def _rank_key(seed: str, fixture_id: str) -> tuple[str, str]:
    # A hash order, not an RNG: it cannot change between Python versions or platforms.
    return hashlib.sha256(f"{seed}:{fixture_id}".encode()).hexdigest(), fixture_id


def select_fixtures(
    candidates: Sequence[Candidate], seed: str, size: int = TICKET_SIZE
) -> list[Candidate]:
    """The `size` candidates with the smallest hash of (seed, fixture id); stable for any input order."""
    return sorted(candidates, key=lambda c: _rank_key(seed, c.fixture_id))[:size]


def pick_outcome(home: float, draw: float, away: float) -> str:
    """Most probable outcome; ties resolve in HOME_WIN, DRAW, AWAY_WIN order (as the summary does)."""
    best = max(home, draw, away)
    return "HOME_WIN" if home == best else "DRAW" if draw == best else "AWAY_WIN"


def shrunk_draw_rate(counts: DrawCounts, competition_id: str | None) -> float:
    """The competition's draw rate pulled toward the global rate by SHRINK_MATCHES prior matches."""
    if counts.total <= 0:
        raise ValueError("no settled history")
    global_rate = counts.draws / counts.total
    played, drawn = counts.by_competition.get(competition_id or "", (0, 0))
    return (drawn + SHRINK_MATCHES * global_rate) / (played + SHRINK_MATCHES)


def draw_score(counts: DrawCounts, candidate: Candidate) -> float:
    """draw-score-v1: shrunk competition draw rate plus the rating model's own draw probability."""
    return shrunk_draw_rate(counts, candidate.competition_id) + candidate.draw


def build_ticket(
    candidates: Sequence[Candidate],
    counts: DrawCounts,
    ticket_date: date,
    frozen_at: datetime,
) -> Ticket | None:
    """The day's ticket, or None when the pool is too small or there is no history to score with."""
    usable = [c for c in candidates if c.kickoff_at > frozen_at]
    if len(usable) < MIN_POOL or counts.total <= 0:
        return None
    seed = ticket_seed(ticket_date)
    chosen = select_fixtures(usable, seed)
    scores = {c.fixture_id: draw_score(counts, c) for c in chosen}
    forced = {
        c.fixture_id
        for c in sorted(chosen, key=lambda c: (-scores[c.fixture_id], c.fixture_id))[:FORCED_DRAWS]
    }
    picks: list[ArmPick] = []
    for c in chosen:
        a_pick = pick_outcome(c.home, c.draw, c.away)
        picks.append(
            ArmPick(c.fixture_id, "A", c.model_name, a_pick, False, None, c.prediction_id, c.kickoff_at)
        )
        is_forced = c.fixture_id in forced
        picks.append(
            ArmPick(
                c.fixture_id,
                "C",
                DRAW_ARM_VERSION,
                "DRAW" if is_forced else a_pick,
                is_forced,
                scores[c.fixture_id],
                None,
                c.kickoff_at,
            )
        )
    return Ticket(
        ticket_id=ticket_id_for(ticket_date),
        ticket_date=ticket_date,
        seed=seed,
        pool_size=len(usable),
        frozen_at=frozen_at,
        fixtures=tuple(c.fixture_id for c in chosen),
        picks=tuple(picks),
    )


def run_daily_ticket(store: TicketStore, now: datetime) -> dict[str, int]:
    """Create today's ticket once. Never raises: a failure is counted and the run goes on."""
    summary = {"ticket_created": 0, "ticket_exists": 0, "ticket_skipped": 0, "ticket_errors": 0}
    now = now.astimezone(timezone.utc)
    ticket_date = now.date()
    try:
        if store.ticket_exists(ticket_date, DESIGN_VERSION):
            summary["ticket_exists"] = 1
            return summary
        candidates = store.ticket_pool(now, now + POOL_WINDOW, RATING_MODEL_NAME)
        ticket = build_ticket(candidates, store.draw_counts(), ticket_date, now)
        if ticket is None:
            summary["ticket_skipped"] = 1
            return summary
        store.append_ticket(ticket)
    except Exception:  # noqa: BLE001 -- isolation boundary: a ticket must never block a prediction run
        summary["ticket_errors"] = 1
        return summary
    summary["ticket_created"] = 1
    return summary
