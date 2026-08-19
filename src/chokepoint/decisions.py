"""Decision sentinels and outcome types shared across the evaluation engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from chokepoint.errors import ChokepointError


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

#: Used only to break ties *within* one precedence level — see `pick_decision`.
_SEVERITY_RANK: dict[str, int] = {"high": 2, "medium": 1, "low": 0}


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
    #: Fingerprint of the policy that produced this rule, carried through to the
    #: ledger event and OTEL span so an audit can tell which version of a policy
    #: made a decision. Set by the `Policy.evaluate()` that built the result.
    policy_hash: str | None = None


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

    Ranked first by decision precedence (BLOCK > ESCALATE > ALLOW), so a BLOCK
    from one rule always wins over an ESCALATE from another rule evaluated for
    the same hook. Ties *within* a precedence level are then broken by severity
    (high > medium > low): between two simultaneous BLOCKs, the recorded
    severity used to be whichever rule happened to be registered first, which
    made the ledger's severity field depend on policy ordering.
    """
    if not failing:
        return None
    return max(failing, key=lambda r: (_PRECEDENCE[r.on_fail], _SEVERITY_RANK.get(r.severity, 0)))


class GuardBlocked(ChokepointError):
    """Raised when a tool call is blocked and the interceptor is in `enforce` mode.

    Covers both a direct BLOCK decision and an ESCALATE that was denied or timed
    out (fail-safe: an unresolved escalation behaves like a block).

    `args` holds the `GuardDecision`, not its reason string, so the exception
    survives `pickle`/`copy` intact — agents routinely marshal exceptions across
    process boundaries (Celery, `concurrent.futures`, multiprocessing), and a
    round trip used to rebuild `self.decision` as a bare `str`, turning any
    later `exc.decision.reason` into an `AttributeError`. `__str__` still
    renders just the reason, so existing `str(exc)` output is unchanged.
    """

    def __init__(self, decision: GuardDecision) -> None:
        self.decision = decision
        super().__init__(decision)

    def __str__(self) -> str:
        return self.decision.reason
