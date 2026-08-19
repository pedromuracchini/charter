"""Safe production rollout: run a new policy in `dry_run` mode first — it
evaluates and everything is logged to the ledger, but tool calls are never
actually blocked or undone — before flipping the same policy to `enforce`.

Run directly:

    uv run python examples/dry_run_rollout.py
"""

from __future__ import annotations

from chokepoint import BLOCK, ActionLedger, ChokepointInterceptor, GuardBlocked, PolicySet

suspicious_amount_policy = PolicySet("suspicious_amount_check")
suspicious_amount_policy.require(
    lambda ctx: ctx.args["amount"] < 10_000,
    on_fail=BLOCK,
    reason="amount exceeds the new fraud-detection threshold",
)


def transfer(amount: float, to: str) -> dict:
    return {"transferred": amount, "to": to}


def main() -> None:
    ActionLedger.reset()

    # Step 1: dry_run — the policy evaluates and everything is logged, but
    # calls are NEVER actually blocked, no matter what the policy decides.
    dry_run_interceptor = ChokepointInterceptor(policies=[suspicious_amount_policy], mode="dry_run")
    result = dry_run_interceptor.call("transfer", transfer, amount=50_000, to="alice")
    print(f"dry_run: call succeeded despite exceeding the threshold -> {result}")

    events = ActionLedger.current().events()
    would_have_blocked = [e for e in events if e.mode == "dry_run" and e.decision == "BLOCK"]
    print(f"dry_run: {len(would_have_blocked)} call(s) would have been blocked in enforce mode:")
    for event in would_have_blocked:
        print(f"  - {event.tool}({event.args}): {event.reason}")

    # Step 2: satisfied with what dry_run logged, flip the exact same policy
    # to enforce — nothing about the policy itself changes, only the mode.
    enforce_interceptor = ChokepointInterceptor(policies=[suspicious_amount_policy], mode="enforce")
    try:
        enforce_interceptor.call("transfer", transfer, amount=50_000, to="alice")
    except GuardBlocked as exc:
        print(f"enforce: blocked for real -> {exc.decision.reason}")


if __name__ == "__main__":
    main()
