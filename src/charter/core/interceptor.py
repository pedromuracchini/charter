"""`CharterInterceptor` — the integration point between Charter and an agent.

Authorizes every tool call made by one agent. Construct one per agent (the
common pattern — see "Multi-agent" in CLAUDE.md), or share a `registry` across
many interceptors for centralized identity management.

Tool arguments and interceptor options travel separately. `call()` accepts the
tool's arguments as `**kwargs` for brevity, but `session_id` and `domain` are
named parameters of `call()` itself, so a tool that happens to declare an
argument by one of those names cannot be reached that way. Pass `args={...}`
to keep the two namespaces apart; the interceptor warns when it detects the
collision rather than silently dropping the argument.
"""

from __future__ import annotations

import functools
import inspect
import threading
import warnings
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from charter._engine import _maybe_await, evaluate_call, evaluate_call_async
from charter._scope import ExecutionScope, use_scope
from charter.core.decorator import _is_async_tool
from charter.core.policy_set import Policy
from charter.core.reversible import ReversibleAction
from charter.errors import ConfigurationWarning
from charter.ledger.ledger import ActionLedger
from charter.multiagent.registry import CharterRegistry
from charter.redaction import Redactor
from charter.state import CallState

Mode = Literal["enforce", "dry_run", "observe"]

DEFAULT_MAX_SESSIONS = 10_000

#: Distinguishes "the caller passed session_id/domain" from "the caller left it
#: alone", which a plain default can't express — see `_warn_if_shadowed`.
_UNSET: Any = object()

#: Parameters of `call()`/`acall()` that a tool argument of the same name would
#: collide with.
_RESERVED_ARGUMENT_NAMES = ("session_id", "domain")


def _shadowed_parameters(func: Callable[..., Any] | ReversibleAction, names: Iterable[str]) -> list[str]:
    """Which of `names` the target tool declares as parameters of its own.

    A `ReversibleAction` takes one opaque `args` mapping rather than named
    parameters, so there is no signature to check and every name is *possible*.
    That case is handled by `_warn_if_shadowed`, not here.
    """
    if isinstance(func, ReversibleAction):
        return []
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return []
    return [name for name in names if name in parameters]


def _warn_if_shadowed(
    tool_name: str,
    func: Callable[..., Any] | ReversibleAction,
    passed: list[str],
) -> None:
    """Warn when an interceptor option was passed to a tool that declares a
    parameter by the same name.

    `interceptor.call("send", send, domain="example.com")` on a tool whose
    signature is `send(domain, body)` binds `domain` to the *interceptor*, not
    the tool, and the call then fails with a confusing "missing required
    argument". `domain` is an ordinary argument name for a real tool, so this
    is worth surfacing rather than documenting away.

    The check is exact, and deliberately so: it consults the target's real
    signature and fires only on a genuine collision. A `ReversibleAction` has
    no parameter list to consult — its arguments are an opaque mapping that
    could hold a `domain` key — so for one of those, `**kwargs` are its
    arguments and `session_id`/`domain` are interceptor options, by contract
    rather than by inspection. Guessing there was tried and rejected: mixing a
    scope option with tool arguments (`call(name, action, domain="healthcare",
    id=1)`) is a correct, common pattern, and a warning that fires on correct
    code teaches people to ignore warnings. An action whose arguments really do
    need one of those names passes `args={...}`.
    """
    shadowed = _shadowed_parameters(func, passed)
    if not shadowed:
        return
    names = ", ".join(repr(name) for name in shadowed)
    warnings.warn(
        f"{names} was consumed by CharterInterceptor.call() as an interceptor option, "
        f"but may have been meant as an argument for tool {tool_name!r}, which will not "
        f"receive it. Pass the tool's arguments explicitly as args={{...}} to keep the "
        f"two apart.",
        ConfigurationWarning,
        stacklevel=4,
    )


class CharterInterceptor:
    """Authorizes every tool call made by one agent.

    The interceptor is what turns a list of `Policy` objects into enforcement:
    it builds the `ExecutionScope` carrying caller identity, hands the call to
    the evaluation engine, and records the outcome to the ledger.

    Args:
        policies: Policies evaluated on every call. A policy with no
            `active_when` applies to *every* tool routed through this
            interceptor, which is the most common source of surprise — scope
            them with `active_when=lambda ctx: ctx.tool_name == "..."`.
        mode: `"enforce"` blocks, escalates and undoes for real. `"dry_run"`
            and `"observe"` evaluate and record every decision but never block,
            never undo, and never contact an escalation handler.
        registry: Shared identity source. With `agent_id`, populates
            `ctx.caller_role` / `ctx.trust_level` / `ctx.delegation_chain`, and
            appends any policies registered for that agent.
        agent_id: Who this interceptor speaks for. Omitting it leaves
            `ctx.caller_role` permanently `None`, silently defeating any
            `AgentScopedPolicy` with `allowed_roles` — `charter lint` reports
            this as an error.
        otel_tracer: Tracer provider for this interceptor's spans only,
            overriding the one set globally by `configure_otel()`.
        checksum_provider: Backs `ctx.state_checksum_matches()`.
        consent_provider: Backs `ctx.patient_consent_on_file()`.
        max_sessions: How many sessions' step counters to keep before evicting
            the least recently used. `None` for unbounded.
        call_state: Cross-call counters backing rate-limit and budget policies.
            Private per interceptor by default; pass a shared `CallState` to
            enforce one quota across several agents.
        ledger: Where decisions are recorded. Defaults to the process-wide
            `ActionLedger.current()`; pass one explicitly to give a tenant its
            own audit trail inside a shared process.
        redactor: Scrubs arguments and free text at record time. Defaults to
            the process-wide `current_redactor()`.
    """

    def __init__(
        self,
        policies: Iterable[Policy] = (),
        mode: Mode = "enforce",
        *,
        registry: CharterRegistry | None = None,
        agent_id: str | None = None,
        otel_tracer: Any = None,
        checksum_provider: Callable[[], str] | None = None,
        consent_provider: Callable[[str], bool] | None = None,
        max_sessions: int | None = DEFAULT_MAX_SESSIONS,
        call_state: CallState | None = None,
        ledger: ActionLedger | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.policies: list[Policy] = list(policies)
        self.mode: Mode = mode
        self.registry = registry
        self.agent_id = agent_id
        self.otel_tracer = otel_tracer
        self.ledger = ledger
        self.redactor = redactor
        self._checksum_provider = checksum_provider
        self._consent_provider = consent_provider
        # Bounded LRU: a session's step_index resets to 0 if it's evicted after
        # long inactivity — a cosmetic effect only, acceptable to keep this from
        # growing forever on very-long-lived processes with huge session churn.
        self._step_counters: OrderedDict[str, int] = OrderedDict()
        self._max_sessions = max_sessions
        # Backs rate-limit/budget policies. Private per interceptor by default;
        # pass a shared `CallState` to enforce one quota across several agents.
        self.call_state = call_state if call_state is not None else CallState(max_sessions)
        self._wrapped_tools: dict[str, Callable[..., Any]] = {}
        # One interceptor shared across request threads is the normal server
        # shape, and both dicts above are read-modify-written on the hot path.
        # Held only for the dict updates, never across policy evaluation or the
        # tool call itself.
        self._lock = threading.Lock()

        identity = registry.get(agent_id) if (registry is not None and agent_id is not None) else None
        if identity is not None:
            self.policies.extend(identity.policies)

    def __repr__(self) -> str:
        parts = [f"mode={self.mode!r}", f"policies={len(self.policies)}"]
        if self.agent_id is not None:
            parts.insert(0, f"agent_id={self.agent_id!r}")
        with self._lock:
            wrapped = len(self._wrapped_tools)
        if wrapped:
            parts.append(f"wrapped_tools={wrapped}")
        return f"<CharterInterceptor {' '.join(parts)}>"

    def _self_inclusive_chain(self, identity: Any) -> tuple[str, ...]:
        """The delegation chain with this interceptor's own `agent_id` last.

        `CharterRegistry.register(delegation_chain=...)` records *ancestors
        only*, but everything that consumes a chain — `report.graph`'s
        successive-pair edges, `_is_cross_agent`, the ledger's documented
        `["orchestrator", "executor_agent"]` shape — reads it as the full path
        including the acting agent. Under the ancestors-only reading a direct
        parent→child delegation is a one-element tuple, which yields zero graph
        edges and never registers as cross-agent.

        Appending here reconciles the two without changing what callers
        register. `delegation_depth()` counts hops (`len - 1`), so existing
        `max_delegation_depth_policy` thresholds keep their meaning.
        """
        ancestors: tuple[str, ...] = identity.delegation_chain if identity else ()
        if self.agent_id is None:
            return ancestors
        # Tolerate a chain that already ends with us: examples and user code
        # written against the old convention passed self-inclusive chains
        # explicitly to work around the graph bug.
        if ancestors and ancestors[-1] == self.agent_id:
            return ancestors
        return (*ancestors, self.agent_id)

    def _build_scope(self, *, session_id: str, domain: str | None) -> ExecutionScope:
        identity = self.registry.get(self.agent_id) if (self.registry and self.agent_id) else None
        with self._lock:
            step = self._step_counters.get(session_id, 0)
            self._step_counters[session_id] = step + 1
            self._step_counters.move_to_end(session_id)
            if self._max_sessions is not None and len(self._step_counters) > self._max_sessions:
                self._step_counters.popitem(last=False)
        return ExecutionScope(
            session_id=session_id,
            step_index=step,
            domain=domain,
            caller_agent_id=self.agent_id,
            caller_role=identity.role if identity else None,
            delegation_chain=self._self_inclusive_chain(identity),
            trust_level=identity.trust_level if identity else 0,
            checksum_provider=self._checksum_provider,
            consent_provider=self._consent_provider,
            call_state=self.call_state,
        )

    def _prepare(
        self,
        tool_name: str,
        func: Callable[..., Any] | ReversibleAction,
        args: Mapping[str, Any] | None,
        session_id: Any,
        domain: Any,
        kwargs: dict[str, Any],
    ) -> tuple[dict[str, Any], ExecutionScope]:
        """Split interceptor options from tool arguments and build the scope."""
        if args is None:
            _warn_if_shadowed(
                tool_name,
                func,
                [
                    name
                    for name, value in zip(_RESERVED_ARGUMENT_NAMES, (session_id, domain), strict=True)
                    if value is not _UNSET
                ],
            )
            call_args = kwargs
        else:
            call_args = dict(args)
            call_args.update(kwargs)
        scope = self._build_scope(
            session_id="default" if session_id is _UNSET else session_id,
            domain=None if domain is _UNSET else domain,
        )
        return call_args, scope

    def call(
        self,
        tool_name: str,
        func: Callable[..., Any] | ReversibleAction,
        /,
        *,
        args: Mapping[str, Any] | None = None,
        session_id: str = _UNSET,
        domain: str | None = _UNSET,
        **kwargs: Any,
    ) -> Any:
        """Run `func` through this interceptor's policies as tool `tool_name`.

        Args:
            tool_name: The name policies match on and the ledger records.
                Positional-only, so a tool argument may share the name.
            func: The tool to call, or a `ReversibleAction` to run with undo
                support. Positional-only for the same reason.
            args: The tool's arguments, as an explicit mapping. Use this
                whenever a tool declares an argument named `session_id` or
                `domain`, which `**kwargs` cannot express.
            session_id: Groups calls for step counting, rate limits and budgets.
            domain: Ambient domain available to predicates as `ctx.domain`.
            **kwargs: The tool's arguments, for the common case where none of
                them collide with the parameters above.

        Returns:
            Whatever `func` returns.

        Raises:
            GuardBlocked: In `"enforce"` mode, when a policy blocks the call or
                an escalation is denied or times out.
        """
        call_args, scope = self._prepare(tool_name, func, args, session_id, domain, kwargs)
        reversible = func if isinstance(func, ReversibleAction) else None
        with use_scope(scope):
            if reversible is not None:

                def invoke() -> Any:
                    return reversible(call_args)
            else:

                def invoke() -> Any:
                    return func(**call_args)

            return evaluate_call(
                tool_name=tool_name,
                args=call_args,
                invoke=invoke,
                policies=self.policies,
                mode=self.mode,
                scope=scope,
                reversible=reversible,
                tracer_provider=self.otel_tracer,
                ledger=self.ledger,
                redactor=self.redactor,
            )

    async def acall(
        self,
        tool_name: str,
        func: Callable[..., Any] | ReversibleAction,
        /,
        *,
        args: Mapping[str, Any] | None = None,
        session_id: str = _UNSET,
        domain: str | None = _UNSET,
        **kwargs: Any,
    ) -> Any:
        """Async sibling of `call()`, for an `async def` tool (or a
        `ReversibleAction` with an async `do_fn`). Same arguments, same
        semantics, same ledger and OTEL behavior."""
        call_args, scope = self._prepare(tool_name, func, args, session_id, domain, kwargs)
        reversible = func if isinstance(func, ReversibleAction) else None
        with use_scope(scope):
            if reversible is not None:

                async def invoke() -> Any:
                    return await _maybe_await(reversible(call_args))
            else:

                async def invoke() -> Any:
                    return await func(**call_args)

            return await evaluate_call_async(
                tool_name=tool_name,
                args=call_args,
                invoke=invoke,
                policies=self.policies,
                mode=self.mode,
                scope=scope,
                reversible=reversible,
                tracer_provider=self.otel_tracer,
                ledger=self.ledger,
                redactor=self.redactor,
            )

    def wrap_tool(self, tool_name: str, func: Callable[..., Any] | ReversibleAction) -> Callable[..., Any]:
        """Return a callable wrapping `func` through `self.call`/`self.acall`
        (auto-detected — see `_is_async_tool`), suitable for registering in
        place of the original tool on an agent.

        Everything the wrapper is called with is forwarded as tool arguments
        via `args=`, never as interceptor options: an agent framework invoking
        a wrapped tool is passing the model's arguments, so a tool parameter
        named `session_id` or `domain` must reach the tool intact.
        """
        wrapped: Callable[..., Any]

        if _is_async_tool(func):

            async def async_wrapped(**kwargs: Any) -> Any:
                return await self.acall(tool_name, func, args=kwargs)

            wrapped = async_wrapped
        else:

            def sync_wrapped(**kwargs: Any) -> Any:
                return self.call(tool_name, func, args=kwargs)

            wrapped = sync_wrapped

        wrapped.__charter_tool_name__ = tool_name  # type: ignore[union-attr]
        if not isinstance(func, ReversibleAction):
            functools.update_wrapper(wrapped, func)
        else:
            wrapped.__name__ = tool_name
        with self._lock:
            self._wrapped_tools[tool_name] = wrapped
        return wrapped

    @property
    def wrapped_tools(self) -> dict[str, Callable[..., Any]]:
        """A snapshot of the tools wrapped through this interceptor, by name."""
        with self._lock:
            return dict(self._wrapped_tools)

    def use(self, agent: Any) -> Any:
        """One-line integration point: `agent.use(interceptor)` ends up calling
        this. Delegates to `charter.adapters.base.wrap` for tool auto-discovery.
        """
        from charter.adapters.base import wrap as _wrap

        return _wrap(agent, self)
