"""Compute and freeze the arms for a screenshot ticket the Telegram intake stored (docs/INTAKE.md).

Started by the ``ticket_intake`` workflow with one intake id. It reads the parsed rows and the active
rule set, computes arm A (the rating model's pick), arm C (draw-score-v1) and, when a rule set is
active, arm B; writes them append-only before every kickoff in the existing ticket tables; and
replies on Telegram. Odds are used only by arm B's odds-disagreement basis and by the market
comparison line in the reply, never by arm A or arm C. No stakes, no profit claims.

The same inputs always give the same ticket: ties break on the fixture id and the ticket id is
derived from the intake id, so a rerun creates nothing a second time.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from proracle.rating import RATING_MODEL_NAME
from proracle.ticket import (
    DRAW_ARM_VERSION,
    FORCED_DRAWS,
    ArmPick,
    Candidate,
    DrawCounts,
    Ticket,
    draw_score,
    pick_outcome,
)

SHOT_KIND = "SCREENSHOT"
SHOT_DESIGN = "shot-v1"
MAX_RULE_COUNT = 10
_NAMESPACE = uuid.UUID("9b2f6d84-3c1e-4f7a-8d52-6e0a1c4b7f33")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LABEL = {"HOME_WIN": "home", "DRAW": "draw", "AWAY_WIN": "away"}


class TicketModeError(Exception):
    """A safe, fixed message. It never carries a URL, key or row content."""


@dataclass(frozen=True)
class Rule:
    """Arm B's rule set, parsed from the fixed structure the intake stored (version 1)."""

    version: int
    force_draws: int | None = None
    reverse_count: int | None = None
    reverse_basis: str | None = None  # "margin" or "odds_disagreement"
    only_leagues: tuple[str, ...] | None = None
    skip_leagues: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ShotRow:
    position: int
    home_team: str
    away_team: str
    kickoff_at: datetime | None
    odds: tuple[float, float, float] | None
    fixture_id: str | None
    competition_name: str | None


UNMATCHED = "not matched to a stored fixture"
MAX_DROPPED_LISTED = 10


@dataclass(frozen=True)
class Dropped:
    row: ShotRow
    reason: str


@dataclass(frozen=True)
class ShotResult:
    ticket: Ticket
    rows: tuple[ShotRow, ...]  # the rows on the ticket, in ticket order
    dropped: tuple[Dropped, ...]
    market: Mapping[str, tuple[float, float]]  # fixture id -> (model p, de-vigged market p) for arm A's pick
    arm_b_note: str | None


def parse_rule(record: Mapping[str, Any]) -> Rule:
    """Validate the stored structure; an invalid one stops the run rather than being guessed at."""
    structure = record.get("structure")
    if not isinstance(structure, Mapping) or structure.get("v") != 1:
        raise TicketModeError("the active rule has an unknown structure")
    known = {"v", "force_draws", "reverse", "only_leagues", "skip_leagues"}
    if set(structure) - known:
        raise TicketModeError("the active rule has an unknown field")

    def count(block: object, bases: tuple[str, ...]) -> tuple[int, str]:
        if not isinstance(block, Mapping) or set(block) != {"count", "basis"}:
            raise TicketModeError("the active rule has a malformed directive")
        n, basis = block["count"], block["basis"]
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_RULE_COUNT or basis not in bases:
            raise TicketModeError("the active rule has an invalid directive")
        return n, basis

    def names(value: object) -> tuple[str, ...] | None:
        if value is None:
            return None
        if not isinstance(value, list) or not 1 <= len(value) <= 10 or not all(isinstance(v, str) and v for v in value):
            raise TicketModeError("the active rule has an invalid league list")
        return tuple(value)

    force = count(structure["force_draws"], ("draw_score",)) if "force_draws" in structure else None
    reverse = count(structure["reverse"], ("margin", "odds_disagreement")) if "reverse" in structure else None
    only, skip = names(structure.get("only_leagues")), names(structure.get("skip_leagues"))
    if only and skip:
        raise TicketModeError("the active rule uses both only and skip")
    version = record.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise TicketModeError("the active rule has no version")
    return Rule(
        version=version,
        force_draws=force[0] if force else None,
        reverse_count=reverse[0] if reverse else None,
        reverse_basis=reverse[1] if reverse else None,
        only_leagues=only,
        skip_leagues=skip,
    )


def devig(odds: tuple[float, float, float]) -> tuple[float, float, float]:
    """Decimal 1X2 odds to fair probabilities by proportional margin removal."""
    raw = tuple(1.0 / o for o in odds)
    total = sum(raw)
    return (raw[0] / total, raw[1] / total, raw[2] / total)


def _model_probs(c: Candidate) -> tuple[float, float, float]:
    return (c.home, c.draw, c.away)


def _index(pick: str) -> int:
    return {"HOME_WIN": 0, "DRAW": 1, "AWAY_WIN": 2}[pick]


def _margin(c: Candidate) -> float:
    top = sorted(_model_probs(c), reverse=True)
    return top[0] - top[1]


def _in_scope(rule: Rule, name: str | None) -> bool:
    label = (name or "").casefold()
    if rule.only_leagues is not None:
        return label in {n.casefold() for n in rule.only_leagues}
    if rule.skip_leagues is not None:
        return label not in {n.casefold() for n in rule.skip_leagues}
    return True


def parse_rows(records: Sequence[Mapping[str, Any]]) -> list[ShotRow]:
    rows: list[ShotRow] = []
    for r in records:
        kickoff = r.get("kickoff_at")
        odds = (r.get("home_odds"), r.get("draw_odds"), r.get("away_odds"))
        fixture = r.get("fixture")
        name = fixture.get("competition_name") if isinstance(fixture, Mapping) else None
        rows.append(
            ShotRow(
                position=int(r["position"]),
                home_team=str(r["home_team"]),
                away_team=str(r["away_team"]),
                kickoff_at=datetime.fromisoformat(kickoff.replace("Z", "+00:00")) if isinstance(kickoff, str) else None,
                odds=(float(odds[0]), float(odds[1]), float(odds[2])) if all(o is not None for o in odds) else None,
                fixture_id=str(r["matched_fixture_id"]) if r.get("matched_fixture_id") else None,
                competition_name=name if isinstance(name, str) else None,
            )
        )
    return rows


def intake_ticket_id(intake_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"proracle-shot-ticket:{intake_id}"))


def build_shot_ticket(
    *,
    intake_id: str,
    ticket_date: Any,
    rows: Sequence[ShotRow],
    candidates: Mapping[str, Candidate],
    counts: DrawCounts,
    rule: Rule | None,
    frozen_at: datetime,
) -> ShotResult | None:
    """Arms A, C and (with a rule) B for the matched rows that can still be frozen before kickoff."""
    included: list[tuple[ShotRow, Candidate]] = []
    dropped: list[Dropped] = []
    for row in rows:
        if row.fixture_id is None:
            dropped.append(Dropped(row, UNMATCHED))
            continue
        cand = candidates.get(row.fixture_id)
        if cand is None:
            dropped.append(Dropped(row, "no pre-kickoff rating prediction"))
        elif cand.kickoff_at <= frozen_at:
            dropped.append(Dropped(row, "already kicked off"))
        else:
            included.append((row, cand))
    if not included or counts.total <= 0:
        return None

    a_pick = {c.fixture_id: pick_outcome(c.home, c.draw, c.away) for _, c in included}
    scores = {c.fixture_id: draw_score(counts, c) for _, c in included}

    picks: list[ArmPick] = []
    forced_c = {
        f for f, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:FORCED_DRAWS]
    }
    for row, c in included:
        picks.append(ArmPick(c.fixture_id, "A", c.model_name, a_pick[c.fixture_id], False, None, c.prediction_id, c.kickoff_at))
        forced = c.fixture_id in forced_c
        picks.append(
            ArmPick(c.fixture_id, "C", DRAW_ARM_VERSION, "DRAW" if forced else a_pick[c.fixture_id], forced, scores[c.fixture_id], None, c.kickoff_at)
        )

    market: dict[str, tuple[float, float]] = {}
    for row, c in included:
        if row.odds is not None:
            market[c.fixture_id] = (_model_probs(c)[_index(a_pick[c.fixture_id])], devig(row.odds)[_index(a_pick[c.fixture_id])])

    note: str | None = None
    if rule is not None:
        scope = [(row, c) for row, c in included if _in_scope(rule, row.competition_name)]
        chosen: dict[str, tuple[str, str, float | None]] = {}  # fixture id -> (pick, adjustment, score)
        if rule.force_draws:
            ranked = sorted(scope, key=lambda rc: (-scores[rc[1].fixture_id], rc[1].fixture_id))
            for _, c in ranked[: rule.force_draws]:
                chosen[c.fixture_id] = ("DRAW", "FORCED_DRAW", scores[c.fixture_id])
        if rule.reverse_count:
            pool = [(row, c) for row, c in scope if c.fixture_id not in chosen and a_pick[c.fixture_id] != "DRAW"]
            if rule.reverse_basis == "odds_disagreement":
                gap = {c.fixture_id: abs(market[c.fixture_id][0] - market[c.fixture_id][1]) for _, c in pool if c.fixture_id in market}
                ranked_r = sorted((rc for rc in pool if rc[1].fixture_id in gap), key=lambda rc: (-gap[rc[1].fixture_id], rc[1].fixture_id))
                values = gap
            else:
                values = {c.fixture_id: _margin(c) for _, c in pool}
                ranked_r = sorted(pool, key=lambda rc: (values[rc[1].fixture_id], rc[1].fixture_id))
            for _, c in ranked_r[: rule.reverse_count]:
                flipped = "AWAY_WIN" if a_pick[c.fixture_id] == "HOME_WIN" else "HOME_WIN"
                chosen[c.fixture_id] = (flipped, "REVERSED", values[c.fixture_id])
        for _, c in scope:
            pick, adjustment, score = chosen.get(c.fixture_id, (a_pick[c.fixture_id], None, None))
            picks.append(
                ArmPick(c.fixture_id, "B", f"rule-v{rule.version}", pick, False, score, None, c.kickoff_at, adjustment)
            )
        wanted = (rule.force_draws or 0) + (rule.reverse_count or 0)
        done = len(chosen)
        note = f"rule v{rule.version}: {done} of {wanted} requested adjustments applied on {len(scope)} fixture(s) in scope"
        if not scope:
            note = f"rule v{rule.version}: no fixture is in the rule's leagues, so arm B has no picks"

    ordered = [row for row, _ in included]
    ticket = Ticket(
        ticket_id=intake_ticket_id(intake_id),
        ticket_date=ticket_date,
        seed=f"proracle-shot-ticket:{intake_id}",
        pool_size=len(rows),
        frozen_at=frozen_at,
        fixtures=tuple(c.fixture_id for _, c in included),
        picks=tuple(picks),
        design_version=SHOT_DESIGN,
        ticket_kind=SHOT_KIND,
        intake_id=intake_id,
    )
    return ShotResult(ticket=ticket, rows=tuple(ordered), dropped=tuple(dropped), market=market, arm_b_note=note)


def format_reply(result: ShotResult) -> str:
    """Plain text for Telegram: the frozen picks and a model-vs-market line. No stakes or profit claims."""
    picks: dict[str, dict[str, ArmPick]] = {}
    for p in result.ticket.picks:
        picks.setdefault(p.fixture_id, {})[p.arm] = p
    arms = sorted({p.arm for p in result.ticket.picks})
    lines = [f"Frozen before kickoff: {len(result.ticket.fixtures)} fixture(s), arms {', '.join(arms)}."]
    for fixture_id, row in zip(result.ticket.fixtures, result.rows):
        cells = []
        for arm in arms:
            p = picks[fixture_id].get(arm)
            if p is None:
                continue
            mark = " (forced)" if p.forced or p.adjustment == "FORCED_DRAW" else " (reversed)" if p.adjustment == "REVERSED" else ""
            cells.append(f"{arm} {LABEL[p.pick]}{mark}")
        line = f"- {row.home_team} v {row.away_team}: " + " | ".join(cells)
        if fixture_id in result.market:
            model_p, market_p = result.market[fixture_id]
            line += f" [A pick: model {model_p * 100:.0f}% vs market {market_p * 100:.0f}%]"
        lines.append(line)
    if result.arm_b_note:
        lines.append(result.arm_b_note)
    elif "B" not in arms:
        lines.append("No rule is active, so there is no arm B on this ticket.")
    # Rows that never matched a stored fixture were already listed by the first message (the read and
    # match step), so only their number is repeated here; rows that matched but could not be frozen are
    # new information and are listed with the reason.
    unmatched = [d for d in result.dropped if d.reason == UNMATCHED]
    other = [d for d in result.dropped if d.reason != UNMATCHED]
    if unmatched:
        lines.append(f"{len(unmatched)} row(s) not matched to a stored fixture are listed in the message above.")
    if other:
        lines.append(f"Matched but not on the ticket ({len(other)}):")
        lines.extend(f"- {d.row.home_team} v {d.row.away_team}: {d.reason}" for d in other[:MAX_DROPPED_LISTED])
        if len(other) > MAX_DROPPED_LISTED:
            lines.append(f"- and {len(other) - MAX_DROPPED_LISTED} more")
    lines.append("Calibration tracking only: this is not betting advice and says nothing about returns.")
    return "\n".join(lines)


class ShotStore:
    """What run_ticket_mode needs from the database (SupabaseRESTStore provides it)."""

    def intake_ticket(self, intake_id: str) -> Mapping[str, Any] | None: ...
    def intake_rows(self, intake_id: str) -> list[Mapping[str, Any]]: ...
    def screenshot_ticket_exists(self, intake_id: str) -> bool: ...
    def active_rule(self) -> Mapping[str, Any] | None: ...
    def earliest_predictions(self, fixture_ids: Sequence[str], model_name: str) -> dict[str, Candidate]: ...
    def draw_counts(self) -> DrawCounts: ...
    def append_ticket(self, ticket: Ticket) -> None: ...


def run_ticket_mode(
    store: Any, intake_id: str, now: datetime, reply: Callable[[str], None] | None = None
) -> dict[str, int]:
    """Freeze the arms for one stored read. Idempotent: a ticket already written is left alone."""
    if not _UUID.match(intake_id):
        raise TicketModeError("the intake id is not a UUID")
    summary = {"ticket_created": 0, "ticket_exists": 0, "ticket_skipped": 0}
    if store.screenshot_ticket_exists(intake_id):
        summary["ticket_exists"] = 1
        return summary
    intake = store.intake_ticket(intake_id)
    if intake is None:
        raise TicketModeError("no stored read has this intake id")
    now = now.astimezone(timezone.utc)
    rows = parse_rows(store.intake_rows(intake_id))
    matched_ids = [r.fixture_id for r in rows if r.fixture_id]
    candidates = store.earliest_predictions(matched_ids, RATING_MODEL_NAME) if matched_ids else {}
    record = store.active_rule()
    rule = parse_rule(record) if record is not None else None
    result = build_shot_ticket(
        intake_id=intake_id,
        ticket_date=now.date(),
        rows=rows,
        candidates=candidates,
        counts=store.draw_counts(),
        rule=rule,
        frozen_at=now,
    )
    if result is None:
        summary["ticket_skipped"] = 1
        if reply:
            reply("Nothing was frozen: no matched fixture is left that has a rating prediction and has not kicked off.")
        return summary
    store.append_ticket(result.ticket)
    summary["ticket_created"] = 1
    if reply:
        reply(format_reply(result))
    return summary


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Freeze arms A, B and C for a stored screenshot read.")
    parser.add_argument("--intake-id", required=True, help="the intake id the Telegram function stored")
    parser.add_argument("--no-reply", action="store_true", help="do not send the Telegram reply")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env

    from proracle.daily_summary import SummaryError, send_telegram
    from proracle.supabase_adapter import SupabaseRESTStore, SupabaseStoreError

    url, key = env.get("SUPABASE_URL", ""), env.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("PRORACLE ticket mode: SUPABASE_URL and SUPABASE_SERVICE_KEY are required", file=sys.stderr)
        return 2
    token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")

    def reply(text: str) -> None:
        if args.no_reply or not token or not chat:
            return
        try:
            send_telegram(token, chat, text)
        except SummaryError:
            print("PRORACLE ticket mode: the Telegram reply could not be sent", file=sys.stderr)

    try:
        store = SupabaseRESTStore(
            url, key, schema=env.get("PRORACLE_DB_SCHEMA", "proracle"), api_key=env.get("SUPABASE_API_KEY") or None
        )
        summary = run_ticket_mode(store, args.intake_id, datetime.now(timezone.utc), reply)
    except TicketModeError as error:
        print(f"PRORACLE ticket mode stopped: {error}", file=sys.stderr)
        reply("I could not freeze that ticket: " + str(error) + ".")
        return 2
    except SupabaseStoreError as error:
        print(f"PRORACLE ticket mode stopped: {error.error_code}", file=sys.stderr)
        reply("I could not freeze that ticket because the database was unavailable. Nothing was changed.")
        return 3
    print("PRORACLE ticket mode complete: " + " ".join(f"{k}={v}" for k, v in summary.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
