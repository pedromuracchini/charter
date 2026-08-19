"""MCP adapter tests, exercised against real MCP objects over an in-memory
client/server transport — not mocks.

Runs against whichever mcp generation is installed. The three things that
differ are isolated in the shim below, so every test body stays
version-neutral:

- the server class (`FastMCP` on 1.x, `MCPServer` on 2.x),
- how an in-memory `ClientSession` is obtained
  (`create_connected_server_and_client_session` is gone in 2.x; `Client(server)`
  replaces it and exposes the session it drives),
- the error flag on a result (`isError` -> `is_error`).
"""

import contextlib

import pytest

pytest.importorskip("mcp")

from chokepoint import ChokepointInterceptor, wrap
from chokepoint.adapters.mcp import guard_mcp_server, guard_mcp_session
from chokepoint.core.policy_set import PolicySet
from chokepoint.decisions import BLOCK, GuardBlocked
from chokepoint.errors import ConfigurationError
from chokepoint.ledger.ledger import ActionLedger

try:  # mcp >= 2
    from mcp.client import Client
    from mcp.server import MCPServer as _ServerClass

    MCP2 = True
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _ServerClass
    from mcp.shared.memory import create_connected_server_and_client_session

    MCP2 = False


def _server():
    server = _ServerClass("test-server")

    @server.tool()
    def delete_file(path: str) -> str:
        return f"deleted {path}"

    @server.tool()
    def read_file(path: str) -> str:
        return f"contents of {path}"

    return server


@contextlib.asynccontextmanager
async def _session(server):
    """A `ClientSession` connected to `server` over an in-memory transport."""
    if MCP2:
        async with Client(server) as client:
            yield client.session
    else:
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            yield session


def _is_error(result) -> bool:
    """`CallToolResult.isError` (1.x) / `.is_error` (2.x)."""
    return result.is_error if hasattr(result, "is_error") else result.isError


def _blocks_deletes() -> PolicySet:
    policy = PolicySet("no_deletes", active_when=lambda ctx: ctx.tool_name == "delete_file")
    policy.require(lambda ctx: False, on_fail=BLOCK, reason="deleting files is not allowed")
    return policy


# --- client side -----------------------------------------------------------


async def test_client_side_block_raises_and_never_reaches_the_server():
    interceptor = ChokepointInterceptor(policies=[_blocks_deletes()])

    async with _session(_server()) as session:
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
    interceptor = ChokepointInterceptor(policies=[policy])

    async with _session(_server()) as session:
        guard_mcp_session(session, interceptor)
        await session.call_tool("read_file", {"path": "/etc/hosts"})

    assert seen == {"name": "read_file", "args": {"path": "/etc/hosts"}}


async def test_client_side_writes_a_ledger_event():
    interceptor = ChokepointInterceptor(policies=[_blocks_deletes()])

    async with _session(_server()) as session:
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
    interceptor = ChokepointInterceptor(policies=[policy])

    async with _session(_server()) as session:
        guard_mcp_session(session, interceptor)
        guard_mcp_session(session, interceptor)
        await session.call_tool("read_file", {"path": "/x"})

    assert calls == ["read_file"]


async def test_use_auto_detects_a_client_session():
    interceptor = ChokepointInterceptor(policies=[_blocks_deletes()])

    async with _session(_server()) as session:
        assert wrap(session, interceptor) is session
        with pytest.raises(GuardBlocked):
            await session.call_tool("delete_file", {"path": "/x"})


@pytest.mark.skipif(not MCP2, reason="the Client facade is mcp 2.x only")
async def test_wrapping_the_client_facade_guards_its_own_call_tool():
    """`Client.call_tool` delegates to the session on every call, so guarding
    the session underneath covers the facade the 2.x docs hand you."""
    interceptor = ChokepointInterceptor(policies=[_blocks_deletes()])

    async with Client(_server()) as client:
        assert wrap(client, interceptor) is client

        allowed = await client.call_tool("read_file", {"path": "/tmp/x"})
        assert "contents of /tmp/x" in allowed.content[0].text

        with pytest.raises(GuardBlocked, match="deleting files is not allowed"):
            await client.call_tool("delete_file", {"path": "/tmp/x"})


@pytest.mark.skipif(not MCP2, reason="the Client facade is mcp 2.x only")
async def test_wrapping_an_unconnected_client_is_an_explicit_error():
    with pytest.raises(ConfigurationError, match="no session yet"):
        wrap(Client(_server()), ChokepointInterceptor(policies=[]))


# --- server side -----------------------------------------------------------


async def test_server_side_block_returns_an_error_result_not_an_exception():
    """An exception escaping a request handler would tear down the connection
    for every subsequent request — MCP's own error shape is the right answer."""
    interceptor = ChokepointInterceptor(policies=[_blocks_deletes()])
    server = _server()
    guard_mcp_server(server, interceptor)

    async with _session(server) as session:
        result = await session.call_tool("delete_file", {"path": "/etc/passwd"})

    assert _is_error(result) is True
    assert "Blocked by policy" in result.content[0].text
    assert "deleting files is not allowed" in result.content[0].text


async def test_server_side_allows_a_permitted_tool_through_unchanged():
    interceptor = ChokepointInterceptor(policies=[_blocks_deletes()])
    server = _server()
    guard_mcp_server(server, interceptor)

    async with _session(server) as session:
        result = await session.call_tool("read_file", {"path": "/tmp/x"})

    assert _is_error(result) is False
    assert "contents of /tmp/x" in result.content[0].text


async def test_server_side_survives_a_block_and_keeps_serving():
    """The whole reason not to raise: the next request must still work."""
    interceptor = ChokepointInterceptor(policies=[_blocks_deletes()])
    server = _server()
    guard_mcp_server(server, interceptor)

    async with _session(server) as session:
        blocked = await session.call_tool("delete_file", {"path": "/x"})
        assert _is_error(blocked) is True

        after = await session.call_tool("read_file", {"path": "/y"})
        assert _is_error(after) is False


async def test_server_side_records_a_ledger_event():
    interceptor = ChokepointInterceptor(policies=[_blocks_deletes()])
    server = _server()
    guard_mcp_server(server, interceptor)

    async with _session(server) as session:
        await session.call_tool("delete_file", {"path": "/x"})

    event = ActionLedger.current().events()[-1]
    assert event.tool == "delete_file"
    assert event.decision == "BLOCK"


async def test_use_auto_detects_a_server():
    interceptor = ChokepointInterceptor(policies=[_blocks_deletes()])
    server = _server()
    assert wrap(server, interceptor) is server

    async with _session(server) as session:
        result = await session.call_tool("delete_file", {"path": "/x"})
    assert _is_error(result) is True


async def test_wrapping_a_server_twice_does_not_double_evaluate():
    calls = []
    policy = PolicySet("count")
    policy.require(lambda ctx: calls.append(ctx.tool_name) is None, on_fail=BLOCK, reason="count")
    interceptor = ChokepointInterceptor(policies=[policy])
    server = _server()
    guard_mcp_server(server, interceptor)
    guard_mcp_server(server, interceptor)

    async with _session(server) as session:
        await session.call_tool("read_file", {"path": "/x"})

    assert calls == ["read_file"]


def test_guarding_a_server_with_no_tools_handler_is_an_explicit_error():
    from mcp.server.lowlevel import Server

    bare = Server("no-tools")
    with pytest.raises(ConfigurationError, match="define the server's tools"):
        guard_mcp_server(bare, ChokepointInterceptor(policies=[]))


def test_a_bare_server_is_not_claimed_by_the_adapter():
    """`applies_to` must not claim a server with nothing to guard, or
    `wrap()` would raise instead of falling through to another adapter."""
    from mcp.server.lowlevel import Server

    from chokepoint.adapters.mcp import MCPAdapter

    assert MCPAdapter().applies_to(Server("no-tools")) is False
