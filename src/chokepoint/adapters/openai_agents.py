"""OpenAI Agents SDK adapter — wraps `agents.FunctionTool` instances.

`agent` may be either a bare `list[Tool]` — the common shape when wrapping
tools *before* constructing the `Agent`:

    wrapped = chokepoint.wrap(my_tools, interceptor)
    agent = Agent(name="...", tools=wrapped)

— or an `Agent` instance, whose `.tools: list[Tool]` is wrapped in place.
Only `FunctionTool` entries (the ones backed by user code, created via
`@function_tool`) are wrapped; hosted/built-in tools (`WebSearchTool`,
`FileSearchTool`, ...) are passed through unchanged — Chokepoint can't
meaningfully guard tool execution it doesn't control. Registered by default
(see `chokepoint.adapters`); `applies_to()` only attempts the optional `agents`
import lazily, inside the method body.

Note: the SDK has its own `tool_input_guardrails`/`tool_output_guardrails`/
`needs_approval` fields on `FunctionTool`, which run *inside* the SDK's own
run loop. Chokepoint is complementary, not a replacement: it gives you the same
policy/ledger/audit surface across this and every other framework, rather
than an SDK-specific guardrail mechanism.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

from chokepoint.adapters.base import AgentAdapter

if TYPE_CHECKING:
    from chokepoint.core.interceptor import ChokepointInterceptor


def _wrap_tool(tool: Any, interceptor: ChokepointInterceptor) -> Any:
    """Replace `tool.on_invoke_tool` with a version that parses the LLM's
    JSON arguments into `ctx.args` for policy evaluation, then re-serializes
    them to call the *original* `on_invoke_tool` unchanged on approval —
    preserving whatever validation/execution the SDK generated."""
    original_invoke = tool.on_invoke_tool

    async def wrapped_invoke(ctx: Any, args_json: str) -> Any:
        parsed_args = json.loads(args_json) if args_json else {}

        async def inner(**kwargs: Any) -> Any:
            return await original_invoke(ctx, json.dumps(kwargs))

        return await interceptor.acall(tool.name, inner, args=parsed_args)

    return dataclasses.replace(tool, on_invoke_tool=wrapped_invoke)


def _resolve_tools(agent: Any) -> list[Any] | None:
    if isinstance(agent, list):
        return agent
    tools = getattr(agent, "tools", None)
    return tools if isinstance(tools, list) else None


def _is_function_tool(tool: Any) -> bool:
    return hasattr(tool, "on_invoke_tool")


class OpenAIAgentsAdapter(AgentAdapter):
    def applies_to(self, agent: Any) -> bool:
        try:
            import agents  # noqa: F401
        except ImportError:
            return False
        tools = _resolve_tools(agent)
        if not tools:
            return False
        return any(_is_function_tool(t) for t in tools)

    def install(self, agent: Any, interceptor: ChokepointInterceptor) -> Any:
        tools = _resolve_tools(agent)
        if tools is None:
            return agent

        wrapped = [_wrap_tool(t, interceptor) if _is_function_tool(t) else t for t in tools]
        if isinstance(agent, list):
            agent[:] = wrapped
        else:
            agent.tools = wrapped
        return agent
