"""`ReversibleAction` — a do/undo pair with an explicit irreversibility level.

| `irreversibility_level` | Behavior |
|---|---|
| `"low"`       | Executes normally; undo is recorded in the ledger if `undo_fn` is set. |
| `"medium"`    | Executes normally; `undo_fn` is **required** — raised at construction if missing. |
| `"high"`      | Automatically escalates before every execution, to `escalate_to`. |
| `"permanent"` | Unconditionally blocked — the framework refuses to ever call `do_fn`. |

`"high"` needs an `escalate_to` target to be meaningfully different from
`"permanent"`: with no target, `core.escalation.resolve_handler(None)` returns
the `FailSafeEscalationHandler`, which denies — so every call would be blocked.
`tollgate lint` warns about a `"high"` action with no `escalate_to`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from tollgate.decisions import BLOCK, ESCALATE, RuleResult

IrreversibilityLevel = Literal["low", "medium", "high", "permanent"]


class ReversibleAction:
    """Pairs a destructive `do_fn` with an `undo_fn` capable of reverting it.

    Callable directly with a single `args` dict, mirroring how `GuardContext.args`
    is shaped — `action(args)` is equivalent to `action.do_fn(args)`.

    `do_fn`/`undo_fn` may be `def` or `async def` — `@guard` and
    `TollgateInterceptor.acall()` detect an async `do_fn` automatically (see
    `core.decorator._is_async_tool`) and await both appropriately. Calling a
    `ReversibleAction` with an async `do_fn` synchronously (e.g. via the sync
    `TollgateInterceptor.call()`) returns an un-awaited coroutine, which is a
    caller error — use the async entry points for an async `do_fn`.
    `pre_snapshot` stays sync-only (typically cheap local state capture).
    """

    def __init__(
        self,
        do_fn: Callable[[dict[str, Any]], Any],
        undo_fn: Callable[[dict[str, Any], Any], Any] | None,
        name: str,
        irreversibility_level: IrreversibilityLevel = "low",
        pre_snapshot: Callable[[dict[str, Any]], Any] | None = None,
        escalate_to: str | None = None,
        timeout_s: int = 300,
    ) -> None:
        if irreversibility_level == "medium" and undo_fn is None:
            raise ValueError(
                f"ReversibleAction {name!r}: irreversibility_level='medium' requires undo_fn"
            )
        self.do_fn = do_fn
        self.undo_fn = undo_fn
        self.name = name
        self.irreversibility_level: IrreversibilityLevel = irreversibility_level
        self._pre_snapshot = pre_snapshot
        # Where the "high" intrinsic escalation is routed, and how long it may take.
        # Without a target every "high" action resolves to the fail-safe denier and
        # is therefore indistinguishable from "permanent" — see the module docstring.
        self.escalate_to = escalate_to
        self.timeout_s = timeout_s

    def __call__(self, args: dict[str, Any]) -> Any:
        return self.do_fn(args)

    def snapshot(self, args: dict[str, Any]) -> Any:
        """Capture pre-execution state, if a `pre_snapshot` function was given."""
        return self._pre_snapshot(args) if self._pre_snapshot is not None else None

    @property
    def is_undoable(self) -> bool:
        """Whether calling `undo()` would actually revert anything.

        The engine checks this before recording an undo: a `"low"` action with
        no `undo_fn` silently no-ops, and recording `"<name>.undo"` for it would
        put a false success in the audit trail at exactly the moment nothing
        was reverted.
        """
        return self.undo_fn is not None

    def undo(self, args: dict[str, Any], snapshot: Any) -> Any:
        """Revert this action. No-op if no `undo_fn` was configured — check
        `is_undoable` first if the caller needs to know which happened."""
        if self.undo_fn is None:
            return None
        return self.undo_fn(args, snapshot)

    def intrinsic_check(self) -> RuleResult | None:
        """The rule baked into this action's `irreversibility_level`, if any.

        Returned as a synthetic, always-firing `RuleResult` that the evaluation
        engine prepends to a tool call's pre-hook results — `"permanent"` always
        fails closed (BLOCK); `"high"` always requires escalation. `"low"` and
        `"medium"` have no intrinsic check; they run like any other guarded call.
        """
        if self.irreversibility_level == "permanent":
            return RuleResult(
                passed=False,
                on_fail=BLOCK,
                reason=f"{self.name}: permanent actions are never executed",
                policy_name="reversible_action.permanent",
                severity="high",
            )
        if self.irreversibility_level == "high":
            return RuleResult(
                passed=False,
                on_fail=ESCALATE,
                reason=f"{self.name}: high-irreversibility actions require escalation",
                policy_name="reversible_action.high",
                severity="high",
                escalate_to=self.escalate_to,
                timeout_s=self.timeout_s,
            )
        return None
