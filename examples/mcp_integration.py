"""Guarding MCP `tools/call` from both sides, over an in-memory transport.

MCP is how most agents reach tools today, and Tollgate can sit on either end:

- **Client side** — you run the agent and police what it asks any server to
  do. A denial raises `GuardBlocked` into your own calling code.
- **Server side** — you run the server and the policy holds no matter which
  client connects. A denial returns `CallToolResult(isError=True)`, because an
  exception escaping a request handler would tear down the connection for
  every later request.

Requires the extra: `uv sync --extra mcp`.

Run directly:

    uv run python examples/mcp_integration.py
"""

from __future__ import annotations

import asyncio

from mcp import ClientSession  # noqa: F401  (documents the client type being guarded)
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

import tollgate
from tollgate import ESCALATE, GuardBlocked, PolicySet, TollgateInterceptor
from tollgate.policies import path_within

WORKSPACE = "/tmp/agent-workspace"


def build_server() -> FastMCP:
    """An ordinary MCP server. Nothing here knows Tollgate exists."""
    server = FastMCP("files")

    @server.tool()
    def read_file(path: str) -> str:
        return f"<contents of {path}>"

    @server.tool()
    def delete_file(path: str) -> str:
        return f"deleted {path}"

    return server


def build_policies() -> list:
    # Confine every path argument to the workspace: resolves before comparing,
    # so `../..` and symlink escapes are caught too.
    confined = path_within([WORKSPACE], tool_names=("read_file", "delete_file"))

    # Deletion needs a human. Nothing is registered for this scheme, so the
    # fail-safe handler denies — which is the point of the default.
    deletion = PolicySet("deletion_needs_approval", active_when=lambda ctx: ctx.tool_name == "delete_file")
    deletion.require(
        lambda ctx: False,
        on_fail=ESCALATE,
        reason="deleting a file requires human approval",
        escalate_to="slack://ops-approvals",
    )
    return [confined, deletion]


async def client_side() -> None:
    print("\n=== client side: guarding what the agent asks for ===")
    interceptor = TollgateInterceptor(policies=build_policies(), agent_id="file_agent")
    server = build_server()

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        tollgate.wrap(session, interceptor)  # auto-detected as an MCP ClientSession

        result = await session.call_tool("read_file", {"path": f"{WORKSPACE}/notes.md"})
        print(f"  read inside the workspace : allowed -> {result.content[0].text}")

        for name, args in [
            ("read_file", {"path": "/etc/passwd"}),
            ("delete_file", {"path": f"{WORKSPACE}/old.log"}),
        ]:
            try:
                await session.call_tool(name, args)
            except GuardBlocked as exc:
                print(f"  {name}({args['path']}): blocked -> {exc.decision.reason}")


async def server_side() -> None:
    print("\n=== server side: guarding whoever connects ===")
    interceptor = TollgateInterceptor(policies=build_policies(), agent_id="mcp_server")
    server = build_server()
    tollgate.wrap(server, interceptor)  # auto-detected as a FastMCP server

    async with create_connected_server_and_client_session(server._mcp_server) as session:
        for name, args in [
            ("read_file", {"path": f"{WORKSPACE}/notes.md"}),
            ("read_file", {"path": "/etc/passwd"}),
            ("delete_file", {"path": f"{WORKSPACE}/old.log"}),
        ]:
            result = await session.call_tool(name, args)
            verdict = "error " if result.isError else "ok    "
            print(f"  {verdict} {name}({args['path']}) -> {result.content[0].text}")

    # The connection survived every denial — that is why the server side
    # returns an error result instead of raising.
    print("  (connection stayed healthy across all three calls)")


async def main() -> None:
    await client_side()
    await server_side()

    print("\n=== ledger ===")
    for event in tollgate.ActionLedger.current().events():
        print(f"  {event.decision:<8} {event.tool:<12} {event.caller_agent_id:<12} {event.reason[:60]}")


if __name__ == "__main__":
    asyncio.run(main())
