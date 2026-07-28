"""`TollgateInterceptor` — the integration point between Tollgate and an agent.

Authorizes every tool call made by one agent. Construct one per agent (the
common pattern — see "Multi-agent" in CLAUDE.md), or share a `registry` across
many interceptors for centralized identity management. `agent_id` populates
`ctx.caller_role` / `ctx.trust_level` / `ctx.delegation_chain` from the
`registry`; omitting it leaves those fields `None`/`0`/`()`, defeating any
policy that relies on `caller_role` — the linter flags this anti-pattern.

`otel_tracer`, if given, overrides the globally configured OTEL tracer
provider (`configure_otel()`) for this interceptor's spans only — useful when
different agents should export to different tracer providers.
"""

from __future__ import annotations

import functools
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any, Literal

from tollgate._engine import _maybe_await, evaluate_call, evaluate_call_async
from tollgate._scope import ExecutionScope, use_scope
from tollgate.core.decorator import _is_async_tool
from tollgate.core.policy_set import Policy
from tollgate.core.reversible import ReversibleAction
from tollgate.multiagent.registry import TollgateRegistry

Mode = Literal["enforce", "dry_run", "observe"]

DEFAULT_MAX_SESSIONS = 10_000


class TollgateInterceptor:
    def __init__(
        self,
        policies: Iterable[Policy] = (),
        mode: Mode = "enforce",
        registry: TollgateRegistry | None = None,
        agent_id: str | None = None,
        otel_tracer: Any = None,
        checksum_provider: Callable[[], str] | None = None,
        consent_provider: Callable[[str], bool] | None = None,
        max_sessions: int | None = DEFAULT_MAX_SESSIONS,
    ) -> None:
        self.policies: list[Policy] = list(policies)
        self.mode: Mode = mode
        self.registry = registry
        self.agent_id = agent_id
        self.otel_tracer = otel_tracer
        self._checksum_provider = checksum_provider
        self._consent_provider = consent_provider
        # Bounded LRU: a session's step_index resets to 0 if it's evicted after
        # long inactivity — a cosmetic effect only, acceptable to keep this from
        # growing forever on very-long-lived processes with huge session churn.
        self._step_counters: OrderedDict[str, int] = OrderedDict()
        self._max_sessions = max_sessions
        self._wrapped_tools: dict[str, Callable[..., Any]] = {}
        # One interceptor shared across request threads is the normal server
        # shape, and both dicts above are read-modify-written on the hot path.
        # Held only for the dict updates, never across policy evaluation or the
        # tool call itself.
        self._lock = threading.Lock()

        identity = registry.get(agent_id) if (registry is not None and agent_id is not None) else None
        if identity is not None:
            self.policies.extend(identity.policies)

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
            delegation_chain=identity.delegation_chain if identity else (),
            trust_level=identity.trust_level if identity else 0,
            checksum_provider=self._checksum_provider,
            consent_provider=self._consent_provider,
        )

    def call(
        self,
        tool_name: str,
        func: Callable[..., Any] | ReversibleAction,
        *,
        session_id: str = "default",
        domain: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Run `func` (or a `ReversibleAction`) through this interceptor's
        policies, registered as tool `tool_name`, called with `kwargs`."""
        scope = self._build_scope(session_id=session_id, domain=domain)
        reversible = func if isinstance(func, ReversibleAction) else None
        args = kwargs
        with use_scope(scope):
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
                policies=self.policies,
                mode=self.mode,
                scope=scope,
                reversible=reversible,
                tracer_provider=self.otel_tracer,
            )

    async def acall(
        self,
        tool_name: str,
        func: Callable[..., Any] | ReversibleAction,
        *,
        session_id: str = "default",
        domain: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Async sibling of `call()` — run an `async def` tool (or a
        `ReversibleAction` with an async `do_fn`) through this interceptor's
        policies, registered as tool `tool_name`, called with `kwargs`."""
        scope = self._build_scope(session_id=session_id, domain=domain)
        reversible = func if isinstance(func, ReversibleAction) else None
        args = kwargs
        with use_scope(scope):
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
                policies=self.policies,
                mode=self.mode,
                scope=scope,
                reversible=reversible,
                tracer_provider=self.otel_tracer,
            )

    def wrap_tool(self, tool_name: str, func: Callable[..., Any] | ReversibleAction) -> Callable[..., Any]:
        """Return a callable wrapping `func` through `self.call`/`self.acall`
        (auto-detected — see `_is_async_tool`), suitable for registering in
        place of the original tool on an agent."""
        wrapped: Callable[..., Any]

        if _is_async_tool(func):

            async def async_wrapped(**kwargs: Any) -> Any:
                return await self.acall(tool_name, func, **kwargs)

            wrapped = async_wrapped
        else:

            def sync_wrapped(**kwargs: Any) -> Any:
                return self.call(tool_name, func, **kwargs)

            wrapped = sync_wrapped

        wrapped.__tollgate_tool_name__ = tool_name  # type: ignore[union-attr]
        if not isinstance(func, ReversibleAction):
            functools.update_wrapper(wrapped, func)
        else:
            wrapped.__name__ = tool_name
        with self._lock:
            self._wrapped_tools[tool_name] = wrapped
        return wrapped

    @property
    def wrapped_tools(self) -> dict[str, Callable[..., Any]]:
        with self._lock:
            return dict(self._wrapped_tools)

    def use(self, agent: Any) -> Any:
        """One-line integration point: `agent.use(interceptor)` ends up calling
        this. Delegates to `tollgate.adapters.base.wrap` for tool auto-discovery.
        """
        from tollgate.adapters.base import wrap as _wrap

        return _wrap(agent, self)
