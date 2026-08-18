"""`LedgerEvent` — the immutable record structure written for every decision."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from charter.decisions import Severity

#: `ERROR` is not a policy decision — it records that the guarded tool itself
#: raised after being authorized, so an authorized-but-failed call is still
#: visible in the audit trail. Only `LedgerEvent.decision` ever takes it;
#: `Decision`/`GuardDecision` remain BLOCK/ESCALATE/ALLOW.
DecisionLabel = Literal["BLOCK", "ESCALATE", "ALLOW", "ERROR"]

#: `invoke` marks the tool-execution step, between the pre and post hooks.
HookLabel = Literal["pre", "post", "invoke"]


class ContributingRule(BaseModel):
    """One rule that failed for a hook, beyond the single worst one acted on.

    The engine resolves a hook to exactly one `decision` by precedence
    (BLOCK > ESCALATE > ALLOW), but several rules may have failed at once.
    Recording only the winner loses the rest of the picture: an audit asking
    "what else was wrong with this call?" had no way to answer.
    """

    model_config = ConfigDict(frozen=True)

    policy: str
    reason: str
    on_fail: DecisionLabel
    severity: Severity = "medium"
    policy_hash: str | None = None


class LedgerEvent(BaseModel):
    """One immutable entry in the `ActionLedger`.

    Matches the framework's documented audit schema, including the fields that
    are mandatory in multi-agent contexts (`caller_agent_id`, `caller_role`,
    `delegation_chain`, `trust_level`).
    """

    model_config = ConfigDict(frozen=True)

    event_id: str
    ts: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    policy: str | None = None
    decision: DecisionLabel
    reason: str
    severity: Severity = "medium"
    hook: HookLabel = "pre"
    mode: Literal["enforce", "dry_run", "observe"] = "enforce"
    checksum_expected: str | None = None
    checksum_got: str | None = None
    undo_op: str | None = None
    session_id: str = "default"
    step_index: int = 0
    caller_agent_id: str | None = None
    caller_role: str | None = None
    delegation_chain: list[str] = Field(default_factory=list)
    trust_level: int = 0
    policy_hash: str | None = None
    #: Every rule that failed for this hook, including the one `decision`
    #: reflects. Empty for an aggregate ALLOW (nothing failed).
    contributing_rules: list[ContributingRule] = Field(default_factory=list)
    otel_trace_id: str | None = None
    otel_span_id: str | None = None
