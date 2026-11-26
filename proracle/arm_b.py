"""Arm B's rule sets: the fixed structure the Telegram intake stores, validated for the ticket builders.

Up to MAX_ACTIVE_RULES rule sets are active at once. Each one is its own arm B pick set on a ticket
(arm "B", arm_version "rule-v<version>"), computed on the same fixtures as arm A and arm C, so the
scoreboard can compare them like for like. This module only parses and validates; applying a rule
to a ticket lives in proracle.ticket (rule_picks), which owns the pick types.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_ACTIVE_RULES = 5
MAX_RULE_COUNT = 10


class TicketModeError(Exception):
    """A safe, fixed message. It never carries a URL, key or row content."""


@dataclass(frozen=True)
class Rule:
    """One rule set, parsed from the fixed structure the intake stored (version 1)."""

    version: int
    force_draws: int | None = None
    reverse_count: int | None = None
    reverse_basis: str | None = None  # "margin" or "odds_disagreement"
    only_leagues: tuple[str, ...] | None = None
    skip_leagues: tuple[str, ...] | None = None

    @property
    def arm_version(self) -> str:
        return f"rule-v{self.version}"

    @property
    def needs_odds(self) -> bool:
        """True when a directive can only be applied with the market odds from a screenshot."""
        return self.reverse_basis == "odds_disagreement"


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


def parse_active_rules(records: Sequence[Mapping[str, Any]]) -> list[Rule]:
    """Every active rule, oldest version first. More than the allowed number stops the run: a ticket
    computed from a partial set would silently drop some rules' arms for that day."""
    if len(records) > MAX_ACTIVE_RULES:
        raise TicketModeError(f"more than {MAX_ACTIVE_RULES} rules are active")
    rules = sorted((parse_rule(r) for r in records), key=lambda r: r.version)
    if len({r.version for r in rules}) != len(rules):
        raise TicketModeError("two active rules share a version")
    return rules


def in_scope(rule: Rule, name: str | None) -> bool:
    label = (name or "").casefold()
    if rule.only_leagues is not None:
        return label in {n.casefold() for n in rule.only_leagues}
    if rule.skip_leagues is not None:
        return label not in {n.casefold() for n in rule.skip_leagues}
    return True
