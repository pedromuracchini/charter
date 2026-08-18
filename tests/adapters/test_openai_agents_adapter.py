import json

import pytest

agents_sdk = pytest.importorskip("agents")

from agents import Agent, RunConfig, function_tool  # noqa: E402
from agents.tool_context import ToolContext  # noqa: E402

from charter import BLOCK, CharterInterceptor, PolicySet  # noqa: E402
from charter.adapters.openai_agents import OpenAIAgentsAdapter  # noqa: E402
from charter.decisions import GuardBlocked  # noqa: E402


@function_tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


def _policy() -> PolicySet:
    policy = PolicySet("no_negative")
    policy.require(lambda ctx: ctx.args.get("a", 0) >= 0, on_fail=BLOCK, reason="a must be non-negative")
    return policy


async def _invoke(tool, args: dict) -> object:
    args_json = json.dumps(args)
    ctx = ToolContext(
        context=None, tool_name=tool.name, tool_call_id="t", tool_arguments=args_json, run_config=RunConfig()
    )
    return await tool.on_invoke_tool(ctx, args_json)


def test_applies_to_bare_tool_list():
    assert OpenAIAgentsAdapter().applies_to([add]) is True


def test_applies_to_agent_instance():
    agent = Agent(name="test", tools=[add])
    assert OpenAIAgentsAdapter().applies_to(agent) is True


def test_applies_to_false_for_unrelated_object():
    assert OpenAIAgentsAdapter().applies_to({"tools": "not a list"}) is False
    assert OpenAIAgentsAdapter().applies_to([]) is False


async def test_install_wraps_bare_list_in_place():
    interceptor = CharterInterceptor(policies=[_policy()])
    tools = [add]

    result = OpenAIAgentsAdapter().install(tools, interceptor)

    assert result is tools
    assert tools[0].name == "add"
    assert await _invoke(tools[0], {"a": 1, "b": 2}) == 3
    with pytest.raises(GuardBlocked):
        await _invoke(tools[0], {"a": -1, "b": 2})


async def test_install_wraps_agent_tools_in_place():
    interceptor = CharterInterceptor(policies=[_policy()])
    agent = Agent(name="test", tools=[add])

    result = OpenAIAgentsAdapter().install(agent, interceptor)

    assert result is agent
    assert await _invoke(agent.tools[0], {"a": 5, "b": 5}) == 10
    with pytest.raises(GuardBlocked):
        await _invoke(agent.tools[0], {"a": -5, "b": 5})


async def test_use_auto_registers_openai_agents_adapter_without_manual_registration():
    """OpenAIAgentsAdapter is registered by default via `charter.adapters`
    (imported the first time `interceptor.use()`/`charter.wrap()` runs) — no
    explicit `register_adapter()` call should be required."""
    interceptor = CharterInterceptor(policies=[_policy()])
    tools = [add]

    wrapped = interceptor.use(tools)

    assert await _invoke(wrapped[0], {"a": 1, "b": 2}) == 3
    with pytest.raises(GuardBlocked):
        await _invoke(wrapped[0], {"a": -1, "b": 2})
