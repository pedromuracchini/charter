"""`ReversibleAction`'s four irreversibility levels, side by side.

Run directly:

    uv run python examples/reversible_levels.py
"""

from __future__ import annotations

from chokepoint import (
    ChokepointInterceptor,
    EscalationHandler,
    GuardBlocked,
    ReversibleAction,
    register_handler,
)

_inventory: dict[int, dict] = {1: {"name": "widget", "qty": 10}}

interceptor = ChokepointInterceptor(policies=[])


class ApproveEverything(EscalationHandler):
    """Stands in for a real approval channel so this file runs with no setup.
    See examples/real_escalation_handlers.py for Slack/webhook/CLI."""

    def escalate(self, ctx, rule_result) -> bool:
        print(f"  [escalation] approving {ctx.tool_name!r}: {rule_result.reason}")
        return True


register_handler("demo-approvals", ApproveEverything())


def _never_called(args: dict) -> dict:
    raise AssertionError("do_fn must never run for a permanent ReversibleAction")


def demo(action: ReversibleAction, args: dict) -> None:
    print(f"\n--- irreversibility_level={action.irreversibility_level!r} ---")
    try:
        result = interceptor.call(action.name, action, **args)
        print(f"  executed: {result}")
    except GuardBlocked as exc:
        print(f"  blocked: {exc.decision.reason}")


def main() -> None:
    # "low": executes normally. undo is available (and recorded in the
    # ledger if a later post-hook ever blocks) — nothing here triggers it.
    low = ReversibleAction(
        do_fn=lambda a: {"updated": a["id"]},
        undo_fn=lambda a, s: None,
        name="update_stock_low",
        irreversibility_level="low",
    )
    demo(low, {"id": 1})

    # "medium": executes normally too, but undo_fn is *required* — omitting
    # it is an error raised at construction time (import-time), not call
    # time. Uncomment to see it:
    #
    #   ReversibleAction(do_fn=lambda a: a, undo_fn=None, name="x", irreversibility_level="medium")
    #   # -> ValueError: ReversibleAction 'x': irreversibility_level='medium' requires undo_fn
    medium = ReversibleAction(
        do_fn=lambda a: {"updated": a["id"]},
        undo_fn=lambda a, s: print(f"  [undo] restoring stock to {s}"),
        name="update_stock_medium",
        irreversibility_level="medium",
        pre_snapshot=lambda a: dict(_inventory[a["id"]]),
    )
    demo(medium, {"id": 1})

    # "high": always escalates before executing, routed to `escalate_to`.
    high = ReversibleAction(
        do_fn=lambda a: {"deleted": a["id"]},
        undo_fn=None,
        name="delete_bucket_high",
        irreversibility_level="high",
        escalate_to="demo-approvals://infra",
    )
    demo(high, {"id": "my-bucket"})

    # ...and the same action with no `escalate_to`: its escalation resolves to
    # the fail-safe handler, which denies, so it behaves exactly like
    # "permanent". `chokepoint lint` warns about this — it is almost never what
    # picking "high" over "permanent" was meant to express.
    unrouted = ReversibleAction(
        do_fn=lambda a: {"deleted": a["id"]},
        undo_fn=None,
        name="delete_bucket_unrouted",
        irreversibility_level="high",
    )
    demo(unrouted, {"id": "my-bucket"})

    # "permanent": unconditional block. do_fn is never called, no matter what.
    permanent = ReversibleAction(
        do_fn=_never_called,
        undo_fn=None,
        name="drop_database",
        irreversibility_level="permanent",
    )
    demo(permanent, {})


if __name__ == "__main__":
    main()
