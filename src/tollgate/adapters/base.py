"""`AgentAdapter` base class and the generic `wrap()` entry point.

Concrete framework adapters (LangChain, LangGraph, CrewAI, Claude Agent SDK,
OpenAI Agents SDK) live alongside this module as thin, optional-import
skeletons and are *not* registered by default in this version — see
CLAUDE.md's "Deferred" section. `wrap()` always falls back to `GenericAdapter`,
which covers the common framework-agnostic shapes.
"""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tollgate.core.interceptor import TollgateInterceptor


class AgentAdapter(ABC):
    """Bridges an agent framework's tool registry to a `TollgateInterceptor`."""

    @abstractmethod
    def applies_to(self, agent: Any) -> bool:
        """Whether this adapter knows how to handle `agent`."""

    @abstractmethod
    def install(self, agent: Any, interceptor: TollgateInterceptor) -> Any:
        """Wrap every tool call on `agent` through `interceptor`.

        Returns `agent` (mutated in place) or a replacement object/callable.
        """


class GenericAdapter(AgentAdapter):
    """Fallback adapter covering common, framework-agnostic shapes:

    - `agent.tools` is a `dict[str, Callable]` -> wrapped in place.
    - `agent.tools` is an iterable of `(name, callable)` pairs -> replaced with
      a wrapped dict, if `agent.tools` is settable.
    - `agent` itself is callable (a single bare tool function) -> returns the
      wrapped callable directly.
    - Otherwise: every public callable attribute on `agent` is wrapped in place.
    """

    def applies_to(self, agent: Any) -> bool:
        return True

    def install(self, agent: Any, interceptor: TollgateInterceptor) -> Any:
        tools = getattr(agent, "tools", None)
        if isinstance(tools, dict):
            for name, func in list(tools.items()):
                tools[name] = interceptor.wrap_tool(name, func)
            return agent

        if tools is not None and isinstance(tools, Iterable) and not isinstance(tools, (str, bytes)):
            wrapped = {name: interceptor.wrap_tool(name, func) for name, func in tools}
            with contextlib.suppress(AttributeError):
                agent.tools = wrapped
            return agent

        if callable(agent):
            return interceptor.wrap_tool(getattr(agent, "__name__", "tool"), agent)

        for attr_name in dir(agent):
            if attr_name.startswith("_"):
                continue
            value = getattr(agent, attr_name, None)
            if callable(value) and not isinstance(value, type):
                try:
                    setattr(agent, attr_name, interceptor.wrap_tool(attr_name, value))
                except AttributeError:
                    continue
        return agent


_ADAPTERS: list[AgentAdapter] = []


def register_adapter(adapter: AgentAdapter) -> None:
    """Register a framework-specific adapter, tried before `GenericAdapter`.

    Most-recently-registered wins: adapters are prepended, so a later
    registration shadows an earlier one that claims the same agent. Registering
    the same adapter type twice replaces the earlier instance rather than
    stacking a duplicate.
    """
    _ADAPTERS[:] = [a for a in _ADAPTERS if type(a) is not type(adapter)]
    _ADAPTERS.insert(0, adapter)


def registered_adapters() -> list[AgentAdapter]:
    """The adapters `wrap()` will try, in the order it tries them."""
    return list(_ADAPTERS)


def reset_adapters() -> None:
    """Drop every registered adapter. Intended for tests — the module-level
    registry is process-global and otherwise leaks between them."""
    _ADAPTERS.clear()


def wrap(agent: Any, interceptor: TollgateInterceptor) -> Any:
    """Wrap `agent`'s tools through `interceptor`.

    Tries adapters registered via `register_adapter()` — most recently
    registered first — falling back to `GenericAdapter`. This is what
    `interceptor.use(agent)` calls, and what the public
    `tollgate.wrap(agent, interceptor)` helper exposes directly.
    """
    for adapter in _ADAPTERS:
        if adapter.applies_to(agent):
            return adapter.install(agent, interceptor)
    return GenericAdapter().install(agent, interceptor)
