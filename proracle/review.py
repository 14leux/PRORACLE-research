"""Automatic post-fixtures review of screenshot tickets (docs/TICKETS.md, "The review").

Started by hand (the ``ticket_review`` workflow has no schedule until Mailu has seen the first report).
Each run finds screenshot tickets frozen after the activation time that are fully settled or 36 hours past
their last kickoff, sends one Telegram report per ticket (at most three per run), and records each review so
the same one is never sent twice. A report never gives stakes, profit or betting advice, and states the time
its results were read, because results can be corrected later.

The Telegram send comes first and the stored review second. The database allows one review per ticket, so a
rerun that finds the row sends nothing. If the row cannot be stored after a send, the run stops at once
(no further sends) so the failure is loud rather than a repeated message.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from proracle.ticket import REVIEW_WAIT, devig, utc_and_eat

MAX_REVIEWS_PER_RUN = 3
MESSAGE_LIMIT = 3800  # Telegram refuses more than 4096 characters; a report is split at line breaks below this
OUTCOME_LABEL = {"HOME_WIN": "home", "DRAW": "draw", "AWAY_WIN": "away"}
_OUTCOME_INDEX = {"HOME_WIN": 0, "DRAW": 1, "AWAY_WIN": 2}
FROZEN_AFTER_SETTING = "PRORACLE_REVIEW_FROZEN_AFTER"


class ReviewError(Exception):
    """A review run could not continue; the message is safe to print (no names, no values)."""


@dataclass(frozen=True)
class Pick:
    fixture_id: str
    arm: str  # "A", "C" or "B3" (arm B of rule v3)
    pick: str
    forced: bool
    adjustment: str | None
    prediction_id: str | None


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    home: str
    away: str
    kickoff_at: datetime | None


@dataclass(frozen=True)
class Review:
    ticket_id: str
    completeness: str  # COMPLETE or PARTIAL
    picks_total: int
    picks_settled: int
    text: str
    summary: Mapping[str, Any]


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _arm_label(arm: str, version: str) -> str:
    return f"B{version.removeprefix('rule-v')}" if arm == "B" else arm


def _arm_order(label: str) -> tuple[str, int]:
    return (label[0], int(label[1:]) if label[1:].isdigit() else 0)


def parse_inputs(inputs: Mapping[str, Any]) -> tuple[list[Fixture], list[Pick]]:
    fixtures: list[Fixture] = []
    for row in inputs["fixtures"]:
        detail = row.get("fixture") or {}
        fixtures.append(
            Fixture(
                fixture_id=str(row["fixture_id"]),
                home=str(detail.get("home_team", "?")),
                away=str(detail.get("away_team", "?")),
                kickoff_at=_parse_time(detail.get("kickoff_at")),
            )
        )
    picks = [
        Pick(
            fixture_id=str(p["fixture_id"]),
            arm=_arm_label(str(p["arm"]), str(p.get("arm_version") or "")),
            pick=str(p["pick"]),
            forced=bool(p.get("forced")),
            adjustment=p.get("adjustment") if isinstance(p.get("adjustment"), str) else None,
            prediction_id=str(p["source_prediction_id"]) if p.get("source_prediction_id") else None,
        )
        for p in inputs["picks"]
    ]
    return fixtures, picks


def picks_complete(fixtures: Sequence[Fixture], picks: Sequence[Pick]) -> bool:
    """True when arms A and C, and every arm present, hold exactly one pick for every fixture.

    The ticket's writes are three requests (ticket, fixtures, picks), so a stopped append can leave some arms
    short. A review is stored once and never redone, so a short set must not be reviewed (IMP-065).
    """
    wanted = {f.fixture_id for f in fixtures}
    by_arm: dict[str, list[str]] = {}
    for p in picks:
        by_arm.setdefault(p.arm, []).append(p.fixture_id)
    if "A" not in by_arm or "C" not in by_arm:
        return False
    return all(len(ids) == len(wanted) and set(ids) == wanted for ids in by_arm.values())


def due_time(header: Mapping[str, Any], fixtures: Sequence[Fixture], picks_kickoffs: Sequence[datetime]) -> datetime | None:
    """When the review falls due: the stored time, else the latest kickoff plus the wait (older tickets)."""
    stored = _parse_time(header.get("review_due_at"))
    if stored is not None:
        return stored
    kickoffs = list(picks_kickoffs) or [f.kickoff_at for f in fixtures if f.kickoff_at is not None]
    return max(kickoffs) + REVIEW_WAIT if kickoffs else None


def is_due(*, all_settled: bool, due_at: datetime | None, now: datetime) -> bool:
    """Fully settled, or past the due time. A ticket with no known due time waits for its results."""
    return all_settled or (due_at is not None and now >= due_at)


def build_review(
    *,
    ticket_id: str,
    frozen_at: datetime | None,
    fixtures: Sequence[Fixture],
    picks: Sequence[Pick],
    outcomes: Mapping[str, str],
    scores: Mapping[str, tuple[int, int]],
    odds: Mapping[str, tuple[float, float, float]],
    model_probs: Mapping[str, tuple[float, float, float]],
    now: datetime,
) -> Review:
    """The report for one ticket from what is stored now. Pure: the same inputs give the same text."""
    settled_ids = [f.fixture_id for f in fixtures if f.fixture_id in outcomes]
    unsettled = [f for f in fixtures if f.fixture_id not in outcomes]
    picks_settled = [p for p in picks if p.fixture_id in outcomes]
    completeness = "COMPLETE" if not unsettled else "PARTIAL"
    arms = sorted({p.arm for p in picks}, key=_arm_order)

    def hit(p: Pick) -> bool:
        return p.pick == outcomes[p.fixture_id]

    head = f"Review of the ticket frozen {utc_and_eat(frozen_at)}: " if frozen_at else "Review of a ticket: "
    head += (
        f"{len(settled_ids)} of {len(fixtures)} fixture(s) settled"
        if unsettled
        else f"all {len(fixtures)} fixture(s) settled"
    )
    lines = [head + ".", f"Results read {utc_and_eat(now)}. Results can still be corrected later."]

    cells = []
    for arm in arms:
        mine = [p for p in picks_settled if p.arm == arm]
        cells.append(f"{arm} {sum(hit(p) for p in mine)}/{len(mine)}")
    lines.append("Hits per arm (settled picks): " + (" | ".join(cells) if cells else "none settled yet") + ".")

    special: dict[tuple[str, str], list[Pick]] = {}
    for p in picks_settled:
        if p.forced or p.adjustment == "FORCED_DRAW":
            special.setdefault((p.arm, "forced draws"), []).append(p)
        elif p.adjustment == "REVERSED":
            special.setdefault((p.arm, "reversed picks"), []).append(p)
    if special:
        lines.append("Forced and reversed picks:")
        for (arm, kind), group in sorted(special.items(), key=lambda kv: (_arm_order(kv[0][0]), kv[0][1])):
            lines.append(f"- {arm} {kind}: {sum(hit(p) for p in group)} of {len(group)} hit")
    else:
        lines.append("Forced and reversed picks: none settled.")

    by_fixture: dict[str, dict[str, Pick]] = {}
    for p in picks:
        by_fixture.setdefault(p.fixture_id, {})[p.arm] = p
    if settled_ids:
        lines.append("Per fixture (actual result, then each arm's pick):")
    for f in fixtures:
        if f.fixture_id not in outcomes:
            continue
        score = scores.get(f.fixture_id)
        actual = OUTCOME_LABEL[outcomes[f.fixture_id]] + (f" {score[0]}-{score[1]}" if score else "")
        arm_cells = []
        for arm in arms:
            p = by_fixture.get(f.fixture_id, {}).get(arm)
            if p is None:
                continue
            tag = " forced" if p.forced or p.adjustment == "FORCED_DRAW" else " reversed" if p.adjustment == "REVERSED" else ""
            arm_cells.append(f"{arm} {OUTCOME_LABEL[p.pick]} {'hit' if hit(p) else 'miss'}{tag}")
        lines.append(f"- {f.home} v {f.away}: {actual} | " + " | ".join(arm_cells))

    compared = []
    for f in fixtures:
        if f.fixture_id not in outcomes or f.fixture_id not in odds:
            continue
        a = by_fixture.get(f.fixture_id, {}).get("A")
        probs = model_probs.get(a.prediction_id) if a and a.prediction_id else None
        if probs is None:
            continue
        index = _OUTCOME_INDEX[outcomes[f.fixture_id]]
        compared.append((probs[index], devig(odds[f.fixture_id])[index]))
    if compared:
        model_avg = sum(m for m, _ in compared) / len(compared)
        market_avg = sum(k for _, k in compared) / len(compared)
        higher = sum(m > k for m, k in compared)
        lines.append(
            f"Model against market ({len(compared)} settled fixture(s) with all three prices printed): "
            f"the model gave the actual result {model_avg * 100:.0f}% on average, the de-vigged market "
            f"{market_avg * 100:.0f}%; the model was higher on {higher} of {len(compared)}. "
            "A small sample says little."
        )
    else:
        lines.append("Model against market: no settled fixture had all three prices printed.")

    if unsettled:
        lines.append(f"Not settled yet ({len(unsettled)} of {len(fixtures)}):")
        for f in unsettled:
            when = f" (kickoff {utc_and_eat(f.kickoff_at)})" if f.kickoff_at else ""
            lines.append(f"- {f.home} v {f.away}{when}")
        lines.append("This review is not repeated when they settle.")
    lines.append("Calibration tracking only: this is not betting advice and says nothing about returns.")

    summary: dict[str, Any] = {
        "as_of": now.astimezone(timezone.utc).isoformat(),
        "fixtures_total": len(fixtures),
        "fixtures_settled": len(settled_ids),
        "arms": {arm: [sum(hit(p) for p in picks_settled if p.arm == arm), sum(p.arm == arm for p in picks_settled)] for arm in arms},
        "market_compared": len(compared),
    }
    return Review(ticket_id, completeness, len(picks), len(picks_settled), "\n".join(lines), summary)


def split_for_telegram(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Whole lines per message, never cut mid-line (a single longer line is cut at the limit)."""
    parts: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def run_review(
    store: Any,
    *,
    now: datetime,
    frozen_after: datetime | None,
    send: Callable[[str], None] | None,
    dry_run: bool,
    show: Callable[[str], None] | None = None,
    max_reviews: int = MAX_REVIEWS_PER_RUN,
) -> dict[str, int]:
    """Review what is due. Counts only; nothing here prints a name. `send` is None only in a dry run."""
    now = now.astimezone(timezone.utc)
    summary = {
        "considered": 0, "not_due": 0, "incomplete": 0, "reviewed": 0, "sent": 0,
        "left_for_next_run": 0, "no_activation_time": 0,
    }
    if frozen_after is None:
        # Without an activation time every old ticket would look like backlog, so nothing is reviewed.
        summary["no_activation_time"] = 1
        return summary
    if not dry_run and send is None:
        raise ReviewError("a real run needs a way to send")
    reviewed = store.reviewed_ticket_ids()
    for header in store.reviewable_tickets(frozen_after):
        ticket_id = str(header["id"])
        if ticket_id in reviewed:
            continue
        if summary["reviewed"] >= max_reviews:
            summary["left_for_next_run"] += 1
            continue
        summary["considered"] += 1
        inputs = store.ticket_review_inputs(ticket_id)
        fixtures, picks = parse_inputs(inputs)
        if not fixtures or len(fixtures) != int(header["fixture_count"]) or not picks_complete(fixtures, picks):
            summary["incomplete"] += 1  # an append that did not finish; it is never scored
            continue
        outcomes = store.current_outcomes([f.fixture_id for f in fixtures])
        kickoffs = [t for t in (_parse_time(p.get("kickoff_at")) for p in inputs["picks"]) if t is not None]
        due_at = due_time(header, fixtures, kickoffs)
        if not is_due(all_settled=all(f.fixture_id in outcomes for f in fixtures), due_at=due_at, now=now):
            summary["not_due"] += 1
            continue
        ids = [f.fixture_id for f in fixtures]
        odds: dict[str, tuple[float, float, float]] = {}
        intake_id = header.get("intake_id")
        if isinstance(intake_id, str):
            for row in store.intake_rows(intake_id):
                values = (row.get("home_odds"), row.get("draw_odds"), row.get("away_odds"))
                if row.get("matched_fixture_id") and all(v is not None for v in values):
                    odds.setdefault(str(row["matched_fixture_id"]), (float(values[0]), float(values[1]), float(values[2])))
        prediction_ids = [p.prediction_id for p in picks if p.arm == "A" and p.prediction_id]
        review = build_review(
            ticket_id=ticket_id,
            frozen_at=_parse_time(header.get("frozen_at")),
            fixtures=fixtures,
            picks=picks,
            outcomes=outcomes,
            scores=store.stored_scores([i for i in ids if i in outcomes]),
            odds=odds,
            model_probs=store.prediction_probabilities(prediction_ids) if prediction_ids else {},
            now=now,
        )
        summary["reviewed"] += 1
        if dry_run:
            if show is not None:
                show(review.text)
            continue
        assert send is not None
        for part in split_for_telegram(review.text):
            send(part)
        summary["sent"] += 1
        try:
            store.append_review(ticket_id, review.completeness, review.picks_total, review.picks_settled, review.summary)
        except Exception:
            # Sent but not recorded: stop now so a rerun cannot repeat more than this one review.
            raise ReviewError("a review was sent but could not be recorded; stopping") from None
    return summary


def _parse_frozen_after(raw: str | None) -> datetime | None:
    if raw is None or raw.strip() == "":
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ReviewError("the activation time is not an ISO date and time") from None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Run the review. Any error this module did not expect is reported by its type alone (exit 5).

    This repository's Actions logs are public and a traceback can quote a stored value (an odds string, a
    name), so nothing but the exception's class name is ever printed (IMP-064).
    """
    try:
        return _main(argv, env)
    except Exception as error:  # noqa: BLE001 - deliberate: no traceback or message may reach a public log
        print(f"PRORACLE review stopped: unexpected {type(error).__name__}", file=sys.stderr)
        return 5


def _main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Review screenshot tickets that are settled or 36 hours past their last kickoff.")
    parser.add_argument("--dry-run", action="store_true", help="work out the reviews but send and record nothing")
    parser.add_argument("--frozen-after", default=None, help="only tickets frozen after this time (ISO); else the setting")
    parser.add_argument("--show-text", action="store_true", help="in a dry run, print the report text (not for public logs)")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env

    from proracle.daily_summary import SummaryError, send_telegram
    from proracle.supabase_adapter import SupabaseRESTStore, SupabaseStoreError

    url, key = env.get("SUPABASE_URL", ""), env.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("PRORACLE review: SUPABASE_URL and SUPABASE_SERVICE_KEY are required", file=sys.stderr)
        return 2
    token, chat = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if not args.dry_run and not (token and chat):
        print("PRORACLE review: a real run needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2
    try:
        frozen_after = _parse_frozen_after(args.frozen_after if args.frozen_after is not None else env.get(FROZEN_AFTER_SETTING))
    except ReviewError as error:
        print(f"PRORACLE review stopped: {error}", file=sys.stderr)
        return 2

    def send(text: str) -> None:
        send_telegram(token, chat, text)

    try:
        store = SupabaseRESTStore(
            url, key, schema=env.get("PRORACLE_DB_SCHEMA", "proracle"), api_key=env.get("SUPABASE_API_KEY") or None
        )
        summary = run_review(
            store,
            now=datetime.now(timezone.utc),
            frozen_after=frozen_after,
            send=None if args.dry_run else send,
            dry_run=args.dry_run,
            show=print if args.show_text else None,
        )
    except ReviewError as error:
        print(f"PRORACLE review stopped: {error}", file=sys.stderr)
        return 4
    except SummaryError:
        print("PRORACLE review stopped: the Telegram send failed; nothing more was sent", file=sys.stderr)
        return 4
    except SupabaseStoreError as error:
        print(f"PRORACLE review stopped: {error.error_code}", file=sys.stderr)
        return 3
    mode = "dry-run" if args.dry_run else "real"
    print(f"PRORACLE review complete ({mode}): " + " ".join(f"{k}={v}" for k, v in summary.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
