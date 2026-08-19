"""`@guard` — attach pre/post predicates to a single tool function.

`ctx.args` is the tool call's arguments as a name → value mapping, mirroring
how agent frameworks pass tool-call arguments as a JSON object. Callers may
still use ordinary positional arguments: the wrapper binds them against the
wrapped function's real signature before building `ctx.args`, so
`inspect.signature()` on a guarded tool stays truthful. That matters because
every framework that introspects a tool to build its JSON schema — LangChain,
the OpenAI Agents SDK, MCP — reads that signature and will emit positional
calls from it.

Argument *defaults* are deliberately not filled in: a predicate written to
check whether an argument was supplied at all must keep seeing it absent.

`func` may be a plain function or a `ReversibleAction`; in the latter case, a
post-hook BLOCK automatically triggers `func.undo(...)` using the snapshot
captured before `func.do_fn` ran. A `ReversibleAction` takes a single `args`
dict rather than named parameters, so it has no signature to bind against and
stays keyword-only.

Bare `@guard()` always runs in `"enforce"` mode and uses whatever
`ExecutionScope` is ambient (the default scope, or one installed via
`chokepoint.session(...)`) — `dry_run`/`observe` modes and agent identity are a
`ChokepointInterceptor` concern.

Async: if `func` (or `func.do_fn`, for a `ReversibleAction`) is a coroutine
function, `@guard` returns an `async def` wrapper — `await` it like you would
the original function. Otherwise it returns today's plain sync wrapper. Same
decorator, same name, no `@guard_async` to remember.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from typing import Any, ParamSpec, Protocol, TypeVar, cast, overload

from chokepoint._engine import _maybe_await, evaluate_call, evaluate_call_async
from chokepoint._scope import current_scope
from chokepoint.core.context import GuardContext
from chokepoint.core.policy_set import PolicySet
from chokepoint.core.reversible import ReversibleAction
from chokepoint.decisions import Decision, Severity
from chokepoint.errors import ConfigurationError

P = ParamSpec("P")
R = TypeVar("R")


def _tool_name(func: Callable[..., Any] | ReversibleAction) -> str:
    """The name policies match on and the ledger records.

    Falls back to the class name for a callable object, which has no
    `__name__` of its own — a plain `func.__name__` raised `AttributeError`
    and took the decorator down before any policy existed to protect it.
    """
    if isinstance(func, ReversibleAction):
        return func.name
    return getattr(func, "__name__", type(func).__name__)


def _is_async_tool(func: Callable[..., Any] | ReversibleAction) -> bool:
    target = func.do_fn if isinstance(func, ReversibleAction) else func
    return inspect.iscoroutinefunction(target)


def _signature_of(func: Callable[..., Any] | ReversibleAction) -> inspect.Signature | None:
    """The signature to bind positional arguments against, or `None` when the
    call has to stay keyword-only (a `ReversibleAction`, or a callable whose
    signature can't be introspected — a builtin, say)."""
    if isinstance(func, ReversibleAction):
        return None
    try:
        return inspect.signature(func)
    except (TypeError, ValueError):
        return None


def _bind(
    signature: inspect.Signature | None,
    tool_name: str,
    call_args: tuple[Any, ...],
    call_kwargs: dict[str, Any],
) -> tuple[dict[str, Any], tuple[Any, ...], dict[str, Any]]:
    """Normalize one call into `(ctx.args, positional, keyword)`.

    `ctx.args` is what policies see, so it is always a flat name → value
    mapping regardless of how the caller passed the arguments. `positional` and
    `keyword` are what the underlying function is actually invoked with, which
    keeps positional-only parameters (`def f(x, /)`) working.
    """
    if signature is None:
        if call_args:
            raise TypeError(
                f"{tool_name}() takes keyword arguments only "
                f"(got {len(call_args)} positional); a ReversibleAction is called "
                f"with a single args mapping"
            )
        return call_kwargs, (), call_kwargs
    bound = signature.bind(*call_args, **call_kwargs)
    arguments = dict(bound.arguments)
    for name, parameter in signature.parameters.items():
        # A `**extra` parameter collects its keywords under its own name. Flatten
        # it, so a predicate reading ctx.args["x"] doesn't have to care whether
        # the tool declared `x` explicitly or swept it up in `**extra`.
        if parameter.kind is inspect.Parameter.VAR_KEYWORD and name in arguments:
            arguments.update(arguments.pop(name))
    return arguments, bound.args, bound.kwargs


class _GuardDecorator(Protocol):
    """What `guard(...)` returns.

    Spelled out as an overloaded `Protocol` rather than a plain `Callable` so
    the wrapped function keeps its own parameter and return types: `@guard` on
    a `(amount: float) -> Receipt` tool must not hand back a `(...) -> Any`.
    Chokepoint ships `py.typed`, so a decorator that erased types would make
    downstream type checking *worse* than not using the library.
    """

    @overload
    def __call__(self, func: ReversibleAction, /) -> Callable[..., Any]: ...

    @overload
    def __call__(self, func: Callable[P, R], /) -> Callable[P, R]: ...


def guard(
    pre: Callable[[GuardContext], bool] | None = None,
    post: Callable[[GuardContext], bool] | None = None,
    *,
    on_fail: Decision,
    reason: str,
    escalate_to: str | None = None,
    timeout_s: int = 300,
    severity: Severity = "medium",
) -> _GuardDecorator:
    """Build a decorator that guards a single tool function (or `ReversibleAction`).

    Args:
        pre: Predicate evaluated *before* the tool runs. Returning `False`
            fails the rule and triggers `on_fail`.
        post: Predicate evaluated *after* the tool runs, with `ctx.result`
            populated. A failing post-hook on a `ReversibleAction` triggers
            its `undo_fn`.
        on_fail: What a failing predicate does — `BLOCK`, `ESCALATE`, or
            `ALLOW` (log-only: recorded to the ledger, never enforced).
        reason: Human-readable explanation, recorded on the ledger event and
            carried in the resulting `GuardBlocked`.
        escalate_to: URI whose scheme selects the registered
            `EscalationHandler`, e.g. `"slack://finance-approvals"`. Only
            meaningful with `on_fail=ESCALATE`.
        timeout_s: How long an escalation handler may take before the call is
            denied (fail-safe). Enforced by the engine, not the handler.
        severity: Recorded on the ledger event and used to break ties between
            rules that fail simultaneously at the same decision level.

    Returns:
        A decorator preserving the wrapped function's signature and types.

    Raises:
        ConfigurationError: If neither `pre` nor `post` is given.
    """
    if pre is None and post is None:
        raise ConfigurationError("@guard requires at least one of pre= or post=")

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
        signature = _signature_of(func)
        wrapper: Callable[..., Any]

        if _is_async_tool(func):

            async def async_wrapper(*call_args: Any, **call_kwargs: Any) -> Any:
                args, positional, keywords = _bind(signature, tool_name, call_args, call_kwargs)
                if reversible is not None:

                    async def invoke() -> Any:
                        return await _maybe_await(reversible(args))
                else:

                    async def invoke() -> Any:
                        return await func(*positional, **keywords)

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

            def sync_wrapper(*call_args: Any, **call_kwargs: Any) -> Any:
                args, positional, keywords = _bind(signature, tool_name, call_args, call_kwargs)
                if reversible is not None:

                    def invoke() -> Any:
                        return reversible(args)
                else:

                    def invoke() -> Any:
                        return func(*positional, **keywords)

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

        wrapper.__chokepoint_policy__ = policy  # type: ignore[union-attr]
        wrapper.__chokepoint_reversible__ = reversible  # type: ignore[union-attr]
        wrapper.__chokepoint_tool_name__ = tool_name  # type: ignore[union-attr]
        if isinstance(func, ReversibleAction):
            wrapper.__name__ = tool_name
        else:
            functools.update_wrapper(wrapper, func)
        return wrapper

    # `decorator` is one function handling both branches of an overloaded
    # protocol; no single non-generic signature expresses "returns exactly what
    # it was given" alongside the ReversibleAction case, so the cast states the
    # contract the overloads above already document.
    return cast(_GuardDecorator, decorator)
