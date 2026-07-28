"""LangChain adapter skeleton — not registered by default.

Concrete tool-wrapping logic against `langchain_core.tools.BaseTool` is
deferred past this version (see CLAUDE.md's "Deferred" section). Until then,
`interceptor.use(agent)` falls back to `GenericAdapter`, which already handles
a plain `agent.tools` mapping. To enable this adapter, implement `install()`
below and call `register_adapter(LangChainAdapter())`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tollgate.adapters.base import AgentAdapter

if TYPE_CHECKING:
    from tollgate.core.interceptor import TollgateInterceptor


class LangChainAdapter(AgentAdapter):
    def applies_to(self, agent: Any) -> bool:
        try:
            import langchain_core  # noqa: F401
        except ImportError:
            return False
        return hasattr(agent, "tools")

    def install(self, agent: Any, interceptor: TollgateInterceptor) -> Any:
        raise NotImplementedError(
            "LangChainAdapter is a skeleton in this version; use "
            "TollgateInterceptor.wrap_tool() directly, or rely on GenericAdapter "
            "via interceptor.use(agent)."
        )
