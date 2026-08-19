"""Quickstart: the smallest possible Chokepoint setup — two `@guard`-decorated
tool functions, no multi-agent identity or registry required.

Run directly:

    uv run python examples/quickstart.py
"""

from __future__ import annotations

from chokepoint import BLOCK, ESCALATE, GuardBlocked, guard


@guard(
    pre=lambda ctx: ctx.args["amount"] < 500,
    on_fail=BLOCK,
    reason="amount exceeds the auto-approval limit",
)
def transfer_funds(amount: float, to: str) -> dict:
    return {"transferred": amount, "to": to}


@guard(
    pre=lambda ctx: ctx.args["amount"] < 1000,
    on_fail=ESCALATE,
    escalate_to="slack://finance-approvals",
    timeout_s=5,
    reason="large transfer requires manual approval",
)
def transfer_funds_large(amount: float, to: str) -> dict:
    return {"transferred": amount, "to": to}


def main() -> None:
    print(transfer_funds(amount=100, to="alice"))

    try:
        transfer_funds(amount=1000, to="bob")
    except GuardBlocked as exc:
        print(f"blocked: {exc.decision.reason}")

    # No handler is registered for the "slack://" scheme — the default
    # FailSafeEscalationHandler denies rather than silently approving.
    # See examples/custom_escalation_handler.py for a working approval flow.
    try:
        transfer_funds_large(amount=2000, to="carol")
    except GuardBlocked as exc:
        print(f"escalation denied: {exc.decision.reason}")


if __name__ == "__main__":
    main()
