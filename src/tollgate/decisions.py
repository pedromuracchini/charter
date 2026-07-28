"""Decision sentinels and outcome types shared across the evaluation engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class Decision(Enum):
    """The three possible outcomes of evaluating a rule against a `GuardContext`."""

    BLOCK = "block"
    ESCALATE = "escalate"
    ALLOW = "allow"


BLOCK = Decision.BLOCK
ESCALATE = Decision.ESCALATE
ALLOW = Decision.ALLOW

#: Decisions ranked by how strongly they should override a less severe one
#: when multiple rules fire for the same hook.
_PRECEDENCE: dict[Decision, int] = {BLOCK: 2, ESCALATE: 1, ALLOW: 0}

Severity = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class RuleResult:
    """The outcome of evaluating a single rule/predicate against a context."""

    passed: bool
    on_fail: Decision
    reason: str
    policy_name: str
    severity: Severity = "medium"
    escalate_to: str | None = None
    timeout_s: int = 300


@dataclass(frozen=True)
class GuardDecision:
    """The final decision reached for a tool call, after evaluating all active rules."""

    action: Decision
    reason: str
    policy_name: str | None = None
    policy_hash: str | None = None
    severity: Severity = "medium"
    rule_results: tuple[RuleResult, ...] = field(default_factory=tuple)
    undo_executed: bool = False


def pick_decision(failing: list[RuleResult]) -> RuleResult | None:
    """Among failing rule results, pick the one to act on.

    Ties are broken by decision precedence (BLOCK > ESCALATE > ALLOW) so that,
    e.g., a BLOCK from one rule always wins over an ESCALATE from another rule
    evaluated for the same hook.
    """
    if not failing:
        return None
    return max(failing, key=lambda r: _PRECEDENCE[r.on_fail])


class GuardBlocked(Exception):
    """Raised when a tool call is blocked and the interceptor is in `enforce` mode.

    Covers both a direct BLOCK decision and an ESCALATE that was denied or timed
    out (fail-safe: an unresolved escalation behaves like a block).
    """

    def __init__(self, decision: GuardDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason)
