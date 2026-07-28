"""`@guard` — attach pre/post predicates to a single tool function.

Guarded functions are called with keyword arguments only, mirroring how agent
frameworks pass tool-call arguments as a JSON object — `ctx.args` *is* those
keyword arguments. `func` may be a plain function or a `ReversibleAction`; in
the latter case, a post-hook BLOCK automatically triggers `func.undo(...)`
using the snapshot captured before `func.do_fn` ran.

Bare `@guard()` always runs in `"enforce"` mode and uses whatever
`ExecutionScope` is ambient (the default scope, or one installed via
`tollgate.session(...)`) — `dry_run`/`observe` modes and agent identity are a
`TollgateInterceptor` concern.

Async: if `func` (or `func.do_fn`, for a `ReversibleAction`) is a coroutine
function, `@guard` returns an `async def` wrapper — `await` it like you would
the original function. Otherwise it returns today's plain sync wrapper. Same
decorator, same name, no `@guard_async` to remember.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any

from tollgate._engine import _maybe_await, evaluate_call, evaluate_call_async
from tollgate._scope import current_scope
from tollgate.core.context import GuardContext
from tollgate.core.policy_set import PolicySet
from tollgate.core.reversible import ReversibleAction
from tollgate.decisions import Decision, Severity


def _tool_name(func: Callable[..., Any] | ReversibleAction) -> str:
    if isinstance(func, ReversibleAction):
        return func.name
    return func.__name__


def _is_async_tool(func: Callable[..., Any] | ReversibleAction) -> bool:
    target = func.do_fn if isinstance(func, ReversibleAction) else func
    return inspect.iscoroutinefunction(target)


def guard(
    pre: Callable[[GuardContext], bool] | None = None,
    post: Callable[[GuardContext], bool] | None = None,
    *,
    on_fail: Decision,
    reason: str,
    escalate_to: str | None = None,
    timeout_s: int = 300,
    severity: Severity = "medium",
) -> Callable[[Callable[..., Any] | ReversibleAction], Callable[..., Any]]:
    """Build a decorator that guards a single tool function (or `ReversibleAction`)."""
    if pre is None and post is None:
        raise ValueError("@guard requires at least one of pre= or post=")

    def decorator(func: Callable[..., Any] | ReversibleAction) -> Callable[..., Any]:
        tool_name = _tool_name(func)
        policy = PolicySet(name=f"{tool_name}.guard")
        if pre is not None:
            policy.require(
                pre,
                on_fail=on_fail,
                reason=reason,
                escalate_to=escalate_to,
                timeout_s=timeout_s,
                severity=severity,
                hook="pre",
            )
        if post is not None:
            policy.require(
                post,
                on_fail=on_fail,
                reason=reason,
                escalate_to=escalate_to,
                timeout_s=timeout_s,
                severity=severity,
                hook="post",
            )

        reversible = func if isinstance(func, ReversibleAction) else None
        wrapper: Callable[..., Any]

        if _is_async_tool(func):

            async def async_wrapper(**kwargs: Any) -> Any:
                args = kwargs
                if reversible is not None:

                    async def invoke() -> Any:
                        return await _maybe_await(reversible(args))
                else:

                    async def invoke() -> Any:
                        return await func(**kwargs)

                return await evaluate_call_async(
                    tool_name=tool_name,
                    args=args,
                    invoke=invoke,
                    policies=[policy],
                    mode="enforce",
                    scope=current_scope(),
                    reversible=reversible,
                )

            wrapper = async_wrapper
        else:

            def sync_wrapper(**kwargs: Any) -> Any:
                args = kwargs
                if reversible is not None:

                    def invoke() -> Any:
                        return reversible(args)
                else:

                    def invoke() -> Any:
                        return func(**kwargs)

                return evaluate_call(
                    tool_name=tool_name,
                    args=args,
                    invoke=invoke,
                    policies=[policy],
                    mode="enforce",
                    scope=current_scope(),
                    reversible=reversible,
                )

            wrapper = sync_wrapper

        wrapper.__tollgate_policy__ = policy  # type: ignore[union-attr]
        wrapper.__tollgate_reversible__ = reversible  # type: ignore[union-attr]
        wrapper.__tollgate_tool_name__ = tool_name  # type: ignore[union-attr]
        if isinstance(func, ReversibleAction):
            wrapper.__name__ = tool_name
        else:
            functools.update_wrapper(wrapper, func)
        return wrapper

    return decorator
