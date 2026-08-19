import pytest

langchain_core = pytest.importorskip("langchain_core")

from langchain_core.tools import tool  # noqa: E402

from chokepoint import BLOCK, ChokepointInterceptor, PolicySet  # noqa: E402
from chokepoint.adapters.langgraph import LangGraphAdapter  # noqa: E402
from chokepoint.decisions import GuardBlocked  # noqa: E402


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


def _policy() -> PolicySet:
    policy = PolicySet("no_negative")
    policy.require(lambda ctx: ctx.args.get("a", 0) >= 0, on_fail=BLOCK, reason="a must be non-negative")
    return policy


def test_applies_to_bare_tool_list():
    assert LangGraphAdapter().applies_to([add]) is True


def test_applies_to_agent_like_object_with_tools_attribute():
    class FakeAgent:
        def __init__(self):
            self.tools = [add]

    assert LangGraphAdapter().applies_to(FakeAgent()) is True


def test_applies_to_false_for_unrelated_object():
    assert LangGraphAdapter().applies_to({"tools": "not a list"}) is False
    assert LangGraphAdapter().applies_to([]) is False


def test_install_wraps_bare_list_in_place():
    interceptor = ChokepointInterceptor(policies=[_policy()])
    tools = [add]

    result = LangGraphAdapter().install(tools, interceptor)

    assert result is tools
    assert tools[0].name == "add"
    assert tools[0].invoke({"a": 1, "b": 2}) == 3
    with pytest.raises(GuardBlocked):
        tools[0].invoke({"a": -1, "b": 2})


def test_install_wraps_tools_attribute_in_place():
    class FakeAgent:
        def __init__(self):
            self.tools = [add]

    interceptor = ChokepointInterceptor(policies=[_policy()])
    agent = FakeAgent()

    result = LangGraphAdapter().install(agent, interceptor)

    assert result is agent
    assert agent.tools[0].invoke({"a": 5, "b": 5}) == 10
    with pytest.raises(GuardBlocked):
        agent.tools[0].invoke({"a": -5, "b": 5})


async def test_install_wraps_async_path():
    interceptor = ChokepointInterceptor(policies=[_policy()])
    tools = [add]

    LangGraphAdapter().install(tools, interceptor)

    assert await tools[0].ainvoke({"a": 2, "b": 3}) == 5
    with pytest.raises(GuardBlocked):
        await tools[0].ainvoke({"a": -2, "b": 3})


def test_use_auto_registers_langgraph_adapter_without_manual_registration():
    """LangGraphAdapter is registered by default via `chokepoint.adapters`
    (imported the first time `interceptor.use()`/`chokepoint.wrap()` runs) —
    no explicit `register_adapter()` call should be required."""
    interceptor = ChokepointInterceptor(policies=[_policy()])
    tools = [add]

    wrapped = interceptor.use(tools)

    assert wrapped[0].invoke({"a": 1, "b": 2}) == 3
    with pytest.raises(GuardBlocked):
        wrapped[0].invoke({"a": -1, "b": 2})
