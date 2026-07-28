"""Auto-registers the framework adapters that have real implementations.

Safe to import unconditionally: `LangGraphAdapter`/`OpenAIAgentsAdapter` only
*attempt* their optional imports lazily, inside `applies_to()` — importing
this package (which happens the first time `TollgateInterceptor.use()` or
`tollgate.wrap()` is called) never requires `langchain_core`/`langgraph`/
`agents` to be installed. `LangChainAdapter`/`CrewAIAdapter`/`ClaudeSDKAdapter`
remain skeletons and are not registered (see CLAUDE.md's "Deferred" section).
"""

from __future__ import annotations

from tollgate.adapters.base import register_adapter
from tollgate.adapters.langgraph import LangGraphAdapter
from tollgate.adapters.openai_agents import OpenAIAgentsAdapter


def register_default_adapters() -> None:
    """(Re-)register the adapters that ship with real implementations.

    Runs once on import. Exposed as a function so `reset_adapters()` has
    something to restore: this module's import side effect can't fire twice,
    so a test that clears the registry would otherwise leave every later test
    without the built-in adapters.
    """
    register_adapter(LangGraphAdapter())
    register_adapter(OpenAIAgentsAdapter())


register_default_adapters()
