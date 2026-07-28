"""CrewAI adapter skeleton — not registered by default.

See `tollgate.adapters.langchain` for the pattern this follows and CLAUDE.md's
"Deferred" section for why concrete logic isn't implemented yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tollgate.adapters.base import AgentAdapter

if TYPE_CHECKING:
    from tollgate.core.interceptor import TollgateInterceptor


class CrewAIAdapter(AgentAdapter):
    def applies_to(self, agent: Any) -> bool:
        try:
            import crewai  # noqa: F401
        except ImportError:
            return False
        return hasattr(agent, "tools")

    def install(self, agent: Any, interceptor: TollgateInterceptor) -> Any:
        raise NotImplementedError(
            "CrewAIAdapter is a skeleton in this version; use "
            "TollgateInterceptor.wrap_tool() directly, or rely on GenericAdapter "
            "via interceptor.use(agent)."
        )
