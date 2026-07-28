"""Shared human-readable escalation summary, reused by every real
`EscalationHandler` implementation under `tollgate.escalation`.

Arguments are redacted before they go out. An escalation message is the least
controlled destination Tollgate writes to — it lands in a Slack channel with
its own membership, or at a webhook endpoint on someone else's infrastructure
— so it gets the same scrubbing the ledger does. See `tollgate.redaction`.

`MAX_ARGS_CHARS` caps the rendered arguments: Slack rejects a message over
40,000 characters outright, so an agent passing a large payload would
otherwise turn every escalation on that tool into a silent delivery failure.
"""

from __future__ import annotations

from tollgate.core.context import GuardContext
from tollgate.decisions import RuleResult
from tollgate.redaction import current_redactor

#: Well under Slack's 40k limit, leaving room for the rest of the summary.
MAX_ARGS_CHARS = 2000


def format_escalation_summary(ctx: GuardContext, rule_result: RuleResult) -> str:
    caller = ctx.caller_agent_id or "unknown"
    role = ctx.caller_role or "no role"
    redactor = current_redactor()

    rendered = str(redactor.redact_args(ctx.args))
    if len(rendered) > MAX_ARGS_CHARS:
        rendered = f"{rendered[:MAX_ARGS_CHARS]}… (truncated, {len(rendered)} chars)"

    return (
        f"Tool: {ctx.tool_name}\n"
        f"Args: {rendered}\n"
        f"Reason: {redactor.redact_text(rule_result.reason)}\n"
        f"Severity: {rule_result.severity}\n"
        f"Policy: {rule_result.policy_name}\n"
        f"Caller: {caller} ({role})\n"
        f"Session: {ctx.session_id}"
    )
