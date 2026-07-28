"""Shared human-readable escalation summary, reused by every real
`EscalationHandler` implementation under `tollgate.escalation`."""

from __future__ import annotations

from tollgate.core.context import GuardContext
from tollgate.decisions import RuleResult


def format_escalation_summary(ctx: GuardContext, rule_result: RuleResult) -> str:
    caller = ctx.caller_agent_id or "unknown"
    role = ctx.caller_role or "no role"
    return (
        f"Tool: {ctx.tool_name}\n"
        f"Args: {ctx.args}\n"
        f"Reason: {rule_result.reason}\n"
        f"Severity: {rule_result.severity}\n"
        f"Policy: {rule_result.policy_name}\n"
        f"Caller: {caller} ({role})\n"
        f"Session: {ctx.session_id}"
    )
