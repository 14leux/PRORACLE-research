"""Daily simulated ticket: a seeded 20-fixture selection with arms A and C (see docs/TICKETS.md).

Nothing here changes a prediction. A ticket is a record of what the arms would have picked,
written before every kickoff on it and never altered, so the arms can be scored against results
later. Arm A is the live rating model's earliest pre-kickoff prediction for the fixture. Arm C
forces the FORCED_DRAWS fixtures with the highest draw score to DRAW and otherwise follows arm A.
Arm B is one pick set per active rule set (up to five at once, each arm_version "rule-v<N>"); a
simulated ticket has no odds, so a rule that needs odds gets no pick set on it.

The selection and the arms are pure functions of the stored rows, the active rules and the date, so
a ticket can be recomputed and checked. No odds, stake or provider payload is used or stored.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol

from proracle.arm_b import Rule, in_scope, parse_active_rules
from proracle.rating import RATING_MODEL_NAME

TICKET_KIND = "SIMULATED"
DESIGN_VERSION = "sim-v1"
DRAW_ARM_VERSION = "draw-score-v1"
TICKET_SIZE = 20
FORCED_DRAWS = 5
# A day's pool below this skips the day. A pool between MIN_POOL and TICKET_SIZE gives a shorter
# ticket (every usable fixture); the ticket's fixture_count records its size. Arm C still forces up
# to FORCED_DRAWS draws, so on a short ticket a larger share of its picks are draws.
MIN_POOL = 10
POOL_WINDOW = timedelta(hours=24)
# Prior weight, in matches, pulling a competition's draw rate toward the global rate.
SHRINK_MATCHES = 40
_NAMESPACE = uuid.UUID("5f0c3d52-7a53-4c5b-9a45-2c7a0b1d9e11")

OUTCOMES = ("HOME_WIN", "DRAW", "AWAY_WIN")
# A ticket's review falls due this long after its last kickoff, or earlier when every fixture has settled.
REVIEW_WAIT = timedelta(hours=36)


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
    # Set for a screenshot ticket: when its last fixture kicks off and when its review falls due (UTC).
    last_kickoff_at: datetime | None = None
    review_due_at: datetime | None = None


class TicketStore(Protocol):
    def ticket_exists(self, ticket_date: date, design_version: str) -> bool: ...

    def ticket_pool(
        self, after: datetime, until: datetime, model_name: str
    ) -> list[Candidate]: ...

    def draw_counts(self) -> DrawCounts: ...

    def active_rules(self) -> list[Mapping[str, Any]]: ...

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


def devig(odds: tuple[float, float, float]) -> tuple[float, float, float]:
    """Decimal 1X2 odds to fair probabilities by proportional margin removal."""
    raw = tuple(1.0 / o for o in odds)
    total = sum(raw)
    return (raw[0] / total, raw[1] / total, raw[2] / total)


def _margin(c: Candidate) -> float:
    top = sorted((c.home, c.draw, c.away), reverse=True)
    return top[0] - top[1]


def rule_picks(
    rule: Rule,
    items: Sequence[tuple[Candidate, str | None]],
    a_pick: Mapping[str, str],
    scores: Mapping[str, float],
    market: Mapping[str, tuple[float, float]],
) -> tuple[list[ArmPick], str]:
    """One rule set's arm B picks for the (candidate, league name) items in its scope, and a note.

    A pick equals arm A's unless a directive adjusted it (FORCED_DRAW or REVERSED), so the rule can
    only differ from arm A on the adjusted picks. `market` maps a fixture id to (model probability,
    de-vigged market probability) of arm A's pick; without it an odds-based reversal has no candidates.
    """
    scope = [c for c, name in items if in_scope(rule, name)]
    chosen: dict[str, tuple[str, str, float | None]] = {}  # fixture id -> (pick, adjustment, score)
    if rule.force_draws:
        ranked = sorted(scope, key=lambda c: (-scores[c.fixture_id], c.fixture_id))
        for c in ranked[: rule.force_draws]:
            chosen[c.fixture_id] = ("DRAW", "FORCED_DRAW", scores[c.fixture_id])
    if rule.reverse_count:
        pool = [c for c in scope if c.fixture_id not in chosen and a_pick[c.fixture_id] != "DRAW"]
        if rule.reverse_basis == "odds_disagreement":
            values: dict[str, float] = {
                c.fixture_id: abs(market[c.fixture_id][0] - market[c.fixture_id][1]) for c in pool if c.fixture_id in market
            }
            ranked_r = sorted((c for c in pool if c.fixture_id in values), key=lambda c: (-values[c.fixture_id], c.fixture_id))
        else:
            values = {c.fixture_id: _margin(c) for c in pool}
            ranked_r = sorted(pool, key=lambda c: (values[c.fixture_id], c.fixture_id))
        for c in ranked_r[: rule.reverse_count]:
            flipped = "AWAY_WIN" if a_pick[c.fixture_id] == "HOME_WIN" else "HOME_WIN"
            chosen[c.fixture_id] = (flipped, "REVERSED", values[c.fixture_id])
    picks: list[ArmPick] = []
    for c in scope:
        pick, adjustment, score = chosen.get(c.fixture_id, (a_pick[c.fixture_id], None, None))
        picks.append(ArmPick(c.fixture_id, "B", rule.arm_version, pick, False, score, None, c.kickoff_at, adjustment))
    wanted = (rule.force_draws or 0) + (rule.reverse_count or 0)
    note = f"rule v{rule.version}: {len(chosen)} of {wanted} requested adjustments applied on {len(scope)} fixture(s) in scope"
    if not scope:
        note = f"rule v{rule.version}: no fixture is in the rule's leagues, so it has no picks"
    return picks, note


def build_ticket(
    candidates: Sequence[Candidate],
    counts: DrawCounts,
    ticket_date: date,
    frozen_at: datetime,
    rules: Sequence[Rule] = (),
) -> Ticket | None:
    """The day's ticket, or None when the pool is too small or there is no history to score with.

    Each rule that needs no odds adds its own arm B pick set; a rule with no fixture in its leagues
    that day adds none."""
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
    a_picks = {c.fixture_id: pick_outcome(c.home, c.draw, c.away) for c in chosen}
    for rule in rules:
        if rule.needs_odds:
            continue  # a simulated ticket has no odds
        rule_pick_list, _note = rule_picks(rule, [(c, c.competition_name) for c in chosen], a_picks, scores, {})
        picks.extend(rule_pick_list)
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
        # The rules are read before anything is written. A ticket is written once a day and never
        # amended, so if the rules cannot be read the ticket waits for the next run instead of
        # being written without the rule arms and quietly skewing the comparison.
        rules = parse_active_rules(store.active_rules())
        candidates = store.ticket_pool(now, now + POOL_WINDOW, RATING_MODEL_NAME)
        ticket = build_ticket(candidates, store.draw_counts(), ticket_date, now, rules)
        if ticket is None:
            summary["ticket_skipped"] = 1
            return summary
        store.append_ticket(ticket)
    except Exception:  # noqa: BLE001 -- isolation boundary: a ticket must never block a prediction run
        summary["ticket_errors"] = 1
        return summary
    summary["ticket_created"] = 1
    return summary


EAT = timezone(timedelta(hours=3), "EAT")  # East Africa Time: UTC+3, no daylight saving


def utc_and_eat(moment: datetime) -> str:
    """One moment in both clocks for messages, e.g. 2030-01-01 12:00 UTC (15:00 EAT)."""
    utc = moment.astimezone(timezone.utc)
    return f"{utc:%Y-%m-%d %H:%M} UTC ({utc.astimezone(EAT):%H:%M} EAT)"
