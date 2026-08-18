"""Real OpenAI Agents SDK integration: wrap `agents.FunctionTool` objects
(created via `@function_tool`) with Charter before handing them to an
`Agent`.

This demonstrates the wrapping and the BLOCK/ALLOW behavior directly through
the real `FunctionTool.on_invoke_tool` entry point — no LLM/API key needed
for that part (a `ToolContext` is constructed by hand, exactly as the SDK's
own `Runner` would build one internally). The commented-out section at the
bottom shows how this plugs into a real agent run.

Requires the `openai-agents` extra: `uv sync --extra openai-agents`.

Run directly:

    uv run python examples/openai_agents_integration.py
"""

from __future__ import annotations

import asyncio
import json

from charter import BLOCK, CharterInterceptor, GuardBlocked, PolicySet


async def _invoke(tool: object, args: dict) -> object:
    """Call `tool.on_invoke_tool` the same way the SDK's own `Runner` does:
    a `ToolContext` plus the arguments as a JSON string."""
    from agents import RunConfig
    from agents.tool_context import ToolContext

    args_json = json.dumps(args)
    ctx = ToolContext(
        context=None,
        tool_name=tool.name,  # type: ignore[attr-defined]
        tool_call_id="example-call",
        tool_arguments=args_json,
        run_config=RunConfig(),
    )
    return await tool.on_invoke_tool(ctx, args_json)  # type: ignore[attr-defined]


async def main() -> None:
    try:
        from agents import Agent, function_tool
    except ImportError:
        print("openai-agents is not installed — run: uv sync --extra openai-agents")
        return

    @function_tool
    def transfer_funds(amount: float, to: str) -> dict:
        """Transfer funds to another account."""
        return {"transferred": amount, "to": to}

    @function_tool
    def search_web(query: str) -> dict:
        """Search the web for a query."""
        return {"results": [f"result for {query!r}"]}

    # active_when scopes this policy to transfer_funds only — a policy
    # without an active_when applies to every tool call through the same
    # interceptor, so an unscoped `ctx.args["amount"]` would raise KeyError
    # on search_web's args (correctly fail-safe-BLOCKed rather than crashing,
    # but not the intended behavior here).
    large_transfer_policy = PolicySet(
        "large_transfer_check", active_when=lambda ctx: ctx.tool_name == "transfer_funds"
    )
    large_transfer_policy.require(
        lambda ctx: ctx.args["amount"] < 500,
        on_fail=BLOCK,
        reason="amount exceeds the auto-approval limit",
    )

    interceptor = CharterInterceptor(policies=[large_transfer_policy])

    # Build the Agent, then wrap its .tools in place — OpenAIAgentsAdapter is
    # registered by default, so interceptor.use(agent) auto-detects it.
    agent = Agent(name="finance_agent", tools=[transfer_funds, search_web])
    interceptor.use(agent)

    print(await _invoke(agent.tools[0], {"amount": 100, "to": "alice"}))
    try:
        await _invoke(agent.tools[0], {"amount": 5000, "to": "bob"})
    except GuardBlocked as exc:
        print(f"blocked: {exc.decision.reason}")

    # search_web has no policy attached, so it's untouched by the guard.
    print(await _invoke(agent.tools[1], {"query": "charter"}))

    # To actually run this with a real model:
    #
    #   from agents import Runner
    #   result = await Runner.run(agent, "transfer $100 to alice")
    #
    # (needs a real OpenAI API key, so not run here.)


if __name__ == "__main__":
    asyncio.run(main())
