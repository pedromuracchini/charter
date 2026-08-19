"""Custom `EscalationHandler`: register a handler for an `escalate_to` URI
scheme so an ESCALATE decision can actually be approved, instead of falling
through to the safe default (see examples/quickstart.py and
examples/clinical.py, where nothing is registered and every escalation is
denied).

Run directly:

    uv run python examples/custom_escalation_handler.py
"""

from __future__ import annotations

from chokepoint import ESCALATE, EscalationHandler, GuardBlocked, guard, register_handler


class SlackApprovalHandler(EscalationHandler):
    """Toy stand-in for a real Slack/webhook integration: approves large
    transfers, denies anything flagged as suspicious."""

    def escalate(self, ctx, rule_result):
        approved = "fraud_flag" not in ctx.args
        verb = "approving" if approved else "denying"
        print(f"  [SlackApprovalHandler] {verb}: {rule_result.reason}")
        return approved


register_handler("demo-slack", SlackApprovalHandler())


@guard(
    pre=lambda ctx: ctx.args["amount"] < 500,
    on_fail=ESCALATE,
    escalate_to="demo-slack://finance-approvals",
    timeout_s=5,
    reason="large transfer requires manual approval",
)
def transfer_funds(amount: float, to: str, fraud_flag: bool = False) -> dict:
    return {"transferred": amount, "to": to}


@guard(
    pre=lambda ctx: False,
    on_fail=ESCALATE,
    escalate_to="unconfigured-scheme://wherever",
    reason="needs approval",
)
def other_tool() -> str:
    return "done"


def main() -> None:
    # Approved: no fraud_flag, so SlackApprovalHandler approves the escalation.
    result = transfer_funds(amount=2000, to="alice")
    print(f"approved escalation: {result}")

    # Denied: fraud_flag present, so SlackApprovalHandler denies it.
    try:
        transfer_funds(amount=2000, to="bob", fraud_flag=True)
    except GuardBlocked as exc:
        print(f"denied escalation: {exc.decision.reason}")

    # No handler registered for this scheme -> falls back to the safe
    # default, which always denies (see CLAUDE.md's "Escalation" section).
    try:
        other_tool()
    except GuardBlocked as exc:
        print(f"unconfigured scheme, denied by default: {exc.decision.reason}")


if __name__ == "__main__":
    main()
