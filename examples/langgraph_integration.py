"""Real LangGraph integration: wrap plain `langchain_core.tools.BaseTool`
objects (the type every LangGraph tool is, whether created via `@tool` or
used through `create_react_agent`/`ToolNode`) with Charter before handing
them to LangGraph.

This demonstrates the wrapping and the BLOCK/ALLOW behavior directly through
the real `BaseTool.invoke()`/`.ainvoke()` entry points — no LLM/API key
needed for that part. The commented-out section at the bottom shows the one
extra line to actually wire this into a running LangGraph agent.

Requires the `langgraph` extra: `uv sync --extra langgraph`.

Run directly:

    uv run python examples/langgraph_integration.py
"""

from __future__ import annotations

import asyncio

from charter import BLOCK, CharterInterceptor, GuardBlocked, PolicySet


def main() -> None:
    try:
        from langchain_core.tools import tool
    except ImportError:
        print("langchain_core is not installed — run: uv sync --extra langgraph")
        return

    @tool
    def transfer_funds(amount: float, to: str) -> dict:
        """Transfer funds to another account."""
        return {"transferred": amount, "to": to}

    @tool
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

    # Wrap the tool list BEFORE handing it to LangGraph. interceptor.use()
    # auto-detects these are BaseTool instances (LangGraphAdapter, registered
    # by default) and returns a new list of wrapped tools with the same
    # name/description/args_schema.
    my_tools = [transfer_funds, search_web]
    wrapped_tools = interceptor.use(my_tools)

    print(wrapped_tools[0].invoke({"amount": 100, "to": "alice"}))
    try:
        wrapped_tools[0].invoke({"amount": 5000, "to": "bob"})
    except GuardBlocked as exc:
        print(f"blocked: {exc.decision.reason}")

    # search_web has no policy attached, so it's untouched by the guard.
    print(asyncio.run(wrapped_tools[1].ainvoke({"query": "charter"})))

    # To actually run this in a LangGraph agent:
    #
    #   from langgraph.prebuilt import create_react_agent
    #   graph = create_react_agent(model, tools=wrapped_tools)
    #   graph.invoke({"messages": [("user", "transfer $100 to alice")]})
    #
    # (needs a real chat model/API key, so not run here.)


if __name__ == "__main__":
    main()
