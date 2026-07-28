"""MCP adapter tests, exercised against real MCP objects over an in-memory
client/server transport — not mocks.
"""

import pytest

pytest.importorskip("mcp")

import mcp.types as types
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from tollgate import TollgateInterceptor, wrap
from tollgate.adapters.mcp import guard_mcp_server, guard_mcp_session
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK, GuardBlocked
from tollgate.errors import ConfigurationError
from tollgate.ledger.ledger import ActionLedger


def _server() -> FastMCP:
    server = FastMCP("test-server")

    @server.tool()
    def delete_file(path: str) -> str:
        return f"deleted {path}"

    @server.tool()
    def read_file(path: str) -> str:
        return f"contents of {path}"

    return server


def _blocks_deletes() -> PolicySet:
    policy = PolicySet("no_deletes", active_when=lambda ctx: ctx.tool_name == "delete_file")
    policy.require(lambda ctx: False, on_fail=BLOCK, reason="deleting files is not allowed")
    return policy


# --- client side -----------------------------------------------------------


async def test_client_side_block_raises_and_never_reaches_the_server():
    interceptor = TollgateInterceptor(policies=[_blocks_deletes()])
    server = _server()

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        guard_mcp_session(session, interceptor)

        allowed = await session.call_tool("read_file", {"path": "/tmp/x"})
        assert "contents of /tmp/x" in allowed.content[0].text

        with pytest.raises(GuardBlocked, match="deleting files is not allowed"):
            await session.call_tool("delete_file", {"path": "/tmp/x"})


async def test_client_side_records_the_tool_arguments_for_policies():
    seen = {}
    policy = PolicySet("capture")
    policy.require(
        lambda ctx: seen.update(name=ctx.tool_name, args=dict(ctx.args)) is None,
        on_fail=BLOCK,
        reason="capture",
    )
    interceptor = TollgateInterceptor(policies=[policy])
    server = _server()

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        guard_mcp_session(session, interceptor)
        await session.call_tool("read_file", {"path": "/etc/hosts"})

    assert seen == {"name": "read_file", "args": {"path": "/etc/hosts"}}


async def test_client_side_writes_a_ledger_event():
    interceptor = TollgateInterceptor(policies=[_blocks_deletes()])
    server = _server()

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        guard_mcp_session(session, interceptor)
        with pytest.raises(GuardBlocked):
            await session.call_tool("delete_file", {"path": "/x"})

    event = ActionLedger.current().events()[-1]
    assert event.tool == "delete_file"
    assert event.decision == "BLOCK"
    assert event.args == {"path": "/x"}


async def test_wrapping_a_session_twice_does_not_double_evaluate():
    """A second wrap would double-count every call against a rate limit."""
    calls = []
    policy = PolicySet("count")
    policy.require(lambda ctx: calls.append(ctx.tool_name) is None, on_fail=BLOCK, reason="count")
    interceptor = TollgateInterceptor(policies=[policy])
    server = _server()

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        guard_mcp_session(session, interceptor)
        guard_mcp_session(session, interceptor)
        await session.call_tool("read_file", {"path": "/x"})

    assert calls == ["read_file"]


async def test_use_auto_detects_a_client_session():
    interceptor = TollgateInterceptor(policies=[_blocks_deletes()])
    server = _server()

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        assert wrap(session, interceptor) is session
        with pytest.raises(GuardBlocked):
            await session.call_tool("delete_file", {"path": "/x"})


# --- server side -----------------------------------------------------------


async def test_server_side_block_returns_an_error_result_not_an_exception():
    """An exception escaping a request handler would tear down the connection
    for every subsequent request — MCP's own error shape is the right answer."""
    interceptor = TollgateInterceptor(policies=[_blocks_deletes()])
    server = _server()
    guard_mcp_server(server, interceptor)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        result = await session.call_tool("delete_file", {"path": "/etc/passwd"})

    assert result.isError is True
    assert "Blocked by policy" in result.content[0].text
    assert "deleting files is not allowed" in result.content[0].text


async def test_server_side_allows_a_permitted_tool_through_unchanged():
    interceptor = TollgateInterceptor(policies=[_blocks_deletes()])
    server = _server()
    guard_mcp_server(server, interceptor)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        result = await session.call_tool("read_file", {"path": "/tmp/x"})

    assert result.isError is False
    assert "contents of /tmp/x" in result.content[0].text


async def test_server_side_survives_a_block_and_keeps_serving():
    """The whole reason not to raise: the next request must still work."""
    interceptor = TollgateInterceptor(policies=[_blocks_deletes()])
    server = _server()
    guard_mcp_server(server, interceptor)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        blocked = await session.call_tool("delete_file", {"path": "/x"})
        assert blocked.isError is True

        after = await session.call_tool("read_file", {"path": "/y"})
        assert after.isError is False


async def test_server_side_records_a_ledger_event():
    interceptor = TollgateInterceptor(policies=[_blocks_deletes()])
    server = _server()
    guard_mcp_server(server, interceptor)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.call_tool("delete_file", {"path": "/x"})

    event = ActionLedger.current().events()[-1]
    assert event.tool == "delete_file"
    assert event.decision == "BLOCK"


async def test_use_auto_detects_a_fastmcp_server():
    interceptor = TollgateInterceptor(policies=[_blocks_deletes()])
    server = _server()
    assert wrap(server, interceptor) is server

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        result = await session.call_tool("delete_file", {"path": "/x"})
    assert result.isError is True


async def test_wrapping_a_server_twice_does_not_double_evaluate():
    calls = []
    policy = PolicySet("count")
    policy.require(lambda ctx: calls.append(ctx.tool_name) is None, on_fail=BLOCK, reason="count")
    interceptor = TollgateInterceptor(policies=[policy])
    server = _server()
    guard_mcp_server(server, interceptor)
    guard_mcp_server(server, interceptor)

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        await session.call_tool("read_file", {"path": "/x"})

    assert calls == ["read_file"]


def test_guarding_a_server_with_no_tools_handler_is_an_explicit_error():
    from mcp.server.lowlevel import Server

    bare = Server("no-tools")
    assert types.CallToolRequest not in bare.request_handlers

    with pytest.raises(ConfigurationError, match="define the server's tools"):
        guard_mcp_server(bare, TollgateInterceptor(policies=[]))
