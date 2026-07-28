"""Audit & compliance reporting: after some guarded calls run, inspect what
happened via `ActionLedger`'s export methods — the framework's "auto
descriptive" promise: policies narrate themselves without a separate docs
file.

Run directly:

    uv run python examples/audit_and_reporting.py
"""

from __future__ import annotations

import contextlib

from tollgate import BLOCK, ActionLedger, GuardBlocked, PolicySet, TollgateInterceptor

policy = PolicySet("large_withdrawal_check")
policy.require(
    lambda ctx: ctx.args["amount"] < 1000,
    on_fail=BLOCK,
    reason="withdrawal exceeds the daily limit",
)


def withdraw(amount: float) -> dict:
    return {"withdrawn": amount}


def main() -> None:
    ActionLedger.reset()
    interceptor = TollgateInterceptor(policies=[policy])

    for amount in [100, 2000, 500, 5000, 50]:
        with contextlib.suppress(GuardBlocked):
            interceptor.call("withdraw", withdraw, amount=amount)

    ledger = ActionLedger.current()

    print("=== compliance report (JSON, truncated) ===")
    print(ledger.export_compliance_report(format="json")[:500], "...\n")

    print("=== policy coverage graph (mermaid) ===")
    print(ledger.export_policy_graph(format="mermaid"))

    print("\n=== narrative (non-technical audience) ===")
    print(ledger.export_narrative(audience="non-technical"))

    print("\n=== auto-generated pytest regression fixtures ===")
    print(ledger.export_fixtures(framework="pytest"))


if __name__ == "__main__":
    main()
