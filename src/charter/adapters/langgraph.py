"""LangGraph adapter — wraps `langchain_core.tools.BaseTool` instances.

LangGraph doesn't have its own tool type: `create_react_agent`, `ToolNode`,
and hand-rolled graphs all consume plain `langchain_core.tools.BaseTool`
objects. "The LangGraph adapter" is really "wrap `BaseTool` objects, the type
LangGraph tools always are" — this also covers any LangChain tool used
outside LangGraph.

`agent` may be either a bare `list[BaseTool]` — the common shape when
wrapping tools *before* constructing the graph:

    wrapped = charter.wrap(my_tools, interceptor)
    graph = create_react_agent(model, tools=wrapped)

— or an object exposing a `.tools: list[BaseTool]` attribute, wrapped in
place. Registered by default (see `charter.adapters`); `applies_to()` only
attempts the optional `langchain_core` import lazily, inside the method body,
so `import charter` never depends on it being installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from charter.adapters.base import AgentAdapter

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from charter.core.interceptor import CharterInterceptor


def _wrap_tool(tool: BaseTool, interceptor: CharterInterceptor) -> BaseTool:
    """Build a new `StructuredTool` that routes through `interceptor` before
    delegating to `tool.invoke`/`tool.ainvoke` — the two entry points every
    `BaseTool` subclass guarantees (part of LangChain's `Runnable` interface),
    so this works regardless of the concrete subclass rather than requiring
    `_run`/`_arun` monkeypatching.
    """
    from langchain_core.tools import StructuredTool

    def sync_call(**kwargs: Any) -> Any:
        def inner(**kw: Any) -> Any:
            return tool.invoke(kw)

        return interceptor.call(tool.name, inner, args=kwargs)

    async def async_call(**kwargs: Any) -> Any:
        async def inner(**kw: Any) -> Any:
            return await tool.ainvoke(kw)

        return await interceptor.acall(tool.name, inner, args=kwargs)

    return StructuredTool.from_function(
        func=sync_call,
        coroutine=async_call,
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        return_direct=tool.return_direct,
        infer_schema=False,
    )


def _resolve_tools(agent: Any) -> list[Any] | None:
    if isinstance(agent, list):
        return agent
    tools = getattr(agent, "tools", None)
    return tools if isinstance(tools, list) else None


class LangGraphAdapter(AgentAdapter):
    def applies_to(self, agent: Any) -> bool:
        try:
            from langchain_core.tools import BaseTool
        except ImportError:
            return False
        tools = _resolve_tools(agent)
        if not tools:
            return False
        return isinstance(tools[0], BaseTool)

    def install(self, agent: Any, interceptor: CharterInterceptor) -> Any:
        from langchain_core.tools import BaseTool

        tools = _resolve_tools(agent)
        if tools is None:
            return agent

        wrapped = [_wrap_tool(t, interceptor) if isinstance(t, BaseTool) else t for t in tools]
        if isinstance(agent, list):
            agent[:] = wrapped
        else:
            agent.tools = wrapped
        return agent
