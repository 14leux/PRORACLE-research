"""Compute and freeze the arms for a screenshot ticket the Telegram intake stored (docs/INTAKE.md).

Started by the ``ticket_intake`` workflow with one intake id. It reads the parsed rows and the
active rule sets, computes arm A (the rating model's pick), arm C (draw-score-v1) and one arm B pick
set per active rule (up to five); writes them append-only before every kickoff in the existing ticket
tables; and replies on Telegram. Odds are used only by a rule's odds-disagreement basis and by the
market comparison line in the reply, never by arm A or arm C. No stakes, no profit claims.

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

from proracle.arm_b import Rule, TicketModeError, parse_active_rules, parse_rule
from proracle.rating import RATING_MODEL_NAME
from proracle.ticket import (
    DRAW_ARM_VERSION,
    FORCED_DRAWS,
    ArmPick,
    Candidate,
    REVIEW_WAIT,
    DrawCounts,
    Ticket,
    devig,
    draw_score,
    pick_outcome,
    rule_picks,
    utc_and_eat,
)

__all__ = ["Rule", "TicketModeError", "parse_rule", "devig"]  # re-exported: the intake tests and callers use them here

SHOT_KIND = "SCREENSHOT"
SHOT_DESIGN = "shot-v1"
_NAMESPACE = uuid.UUID("9b2f6d84-3c1e-4f7a-8d52-6e0a1c4b7f33")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LABEL = {"HOME_WIN": "home", "DRAW": "draw", "AWAY_WIN": "away"}


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
    rule_notes: tuple[str, ...] = ()  # one line per active rule: what it did on this ticket


def _model_probs(c: Candidate) -> tuple[float, float, float]:
    return (c.home, c.draw, c.away)


def _index(pick: str) -> int:
    return {"HOME_WIN": 0, "DRAW": 1, "AWAY_WIN": 2}[pick]


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
    rules: Sequence[Rule],
    frozen_at: datetime,
) -> ShotResult | None:
    """Arms A, C and one arm B pick set per rule for the matched rows that can still be frozen before kickoff."""
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

    notes: list[str] = []
    items = [(c, row.competition_name) for row, c in included]
    for rule in rules:
        rule_pick_list, note = rule_picks(rule, items, a_pick, scores, market)
        picks.extend(rule_pick_list)
        notes.append(note)

    ordered = [row for row, _ in included]
    last_kickoff = max(c.kickoff_at for _, c in included)
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
        last_kickoff_at=last_kickoff,
        review_due_at=last_kickoff + REVIEW_WAIT,
    )
    return ShotResult(
        ticket=ticket, rows=tuple(ordered), dropped=tuple(dropped), market=market, rule_notes=tuple(notes)
    )


def arm_label(pick: ArmPick) -> str:
    """A, C, or B followed by the rule version (arm B of rule v3 is "B3")."""
    return f"B{pick.arm_version.removeprefix('rule-v')}" if pick.arm == "B" else pick.arm


def _arm_order(label: str) -> tuple[str, int]:
    return (label[0], int(label[1:]) if label[1:].isdigit() else 0)


def format_reply(result: ShotResult) -> str:
    """Plain text for Telegram: the frozen picks and a model-vs-market line. No stakes or profit claims."""
    picks: dict[str, dict[str, ArmPick]] = {}
    for p in result.ticket.picks:
        picks.setdefault(p.fixture_id, {})[arm_label(p)] = p
    arms = sorted({arm_label(p) for p in result.ticket.picks}, key=_arm_order)
    lines = [f"Frozen before kickoff: {len(result.ticket.fixtures)} fixture(s), arms {', '.join(arms)}."]
    if result.ticket.last_kickoff_at is not None and result.ticket.review_due_at is not None:
        lines.append(f"Last kickoff: {utc_and_eat(result.ticket.last_kickoff_at)}.")
        lines.append(
            f"Review due: {utc_and_eat(result.ticket.review_due_at)}, or sooner once every fixture has a result."
        )
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
    if result.rule_notes:
        lines.append("Arm B is one pick set per active rule (B3 is rule v3):")
        lines.extend(f"- {note}" for note in result.rule_notes)
    else:
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
    def active_rules(self) -> list[Mapping[str, Any]]: ...
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
    if intake.get("kind") == "SLATE":
        # A slate is stored and matched for testing models only: it never gets frozen picks, so it can never
        # reach the arm scoreboard. The function never dispatches one; this refuses it if one arrives anyway.
        summary["slate_ignored"] = 1
        return summary
    now = now.astimezone(timezone.utc)
    rows = parse_rows(store.intake_rows(intake_id))
    matched_ids = [r.fixture_id for r in rows if r.fixture_id]
    candidates = store.earliest_predictions(matched_ids, RATING_MODEL_NAME) if matched_ids else {}
    rules = parse_active_rules(store.active_rules())
    result = build_shot_ticket(
        intake_id=intake_id,
        ticket_date=now.date(),
        rows=rows,
        candidates=candidates,
        counts=store.draw_counts(),
        rules=rules,
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
