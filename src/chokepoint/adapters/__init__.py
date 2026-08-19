"""Auto-registers the framework adapters that have real implementations.

Safe to import unconditionally: `LangGraphAdapter`/`OpenAIAgentsAdapter`/
`MCPAdapter` only *attempt* their optional imports lazily, inside
`applies_to()` — importing this package (which happens the first time
`ChokepointInterceptor.use()` or `chokepoint.wrap()` is called) never requires
`langchain_core`/`langgraph`/`agents`/`mcp` to be installed.

Anything these three don't claim falls through to `GenericAdapter`, which
handles a plain `agent.tools` dict/iterable or a bare callable. Frameworks
with no dedicated adapter (CrewAI, Claude Agent SDK, AutoGen, ...) are reached
that way, or by calling `interceptor.wrap_tool()` per tool.
"""

from __future__ import annotations

from chokepoint.adapters.base import register_adapter
from chokepoint.adapters.langgraph import LangGraphAdapter
from chokepoint.adapters.mcp import MCPAdapter
from chokepoint.adapters.openai_agents import OpenAIAgentsAdapter


def register_default_adapters() -> None:
    """(Re-)register the adapters that ship with real implementations.

    Runs once on import. Exposed as a function so `reset_adapters()` has
    something to restore: this module's import side effect can't fire twice,
    so a test that clears the registry would otherwise leave every later test
    without the built-in adapters.
    """
    register_adapter(LangGraphAdapter())
    register_adapter(OpenAIAgentsAdapter())
    register_adapter(MCPAdapter())


register_default_adapters()
