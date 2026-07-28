"""Model Context Protocol adapter — guards `tools/call` on either side.

MCP is how most agents reach tools today, and it has two very different
vantage points. Tollgate supports both, because they answer different
questions:

- **Client side** (`guard_mcp_session`) — you run the agent and want to police
  what it asks *any* server to do. Wraps `ClientSession.call_tool`.
- **Server side** (`guard_mcp_server`) — you run the MCP server and want the
  policy enforced no matter which client connects. Wraps the low-level
  `Server`'s `CallToolRequest` handler.

The two differ in how a denial is reported, and the difference is not
cosmetic. A client-side block raises `GuardBlocked` into the caller that
asked for the tool — that code is yours, and an exception is the honest
answer. A server-side block must **not** raise: an exception escaping a
request handler tears down the protocol connection for every subsequent
request. It returns a `CallToolResult(isError=True)` instead, which is exactly
what MCP defines for "this tool call failed" and which the model can read and
react to.

Install the extra to use this: `uv sync --extra mcp`. Registered by default
(see `tollgate.adapters`); `applies_to()` only attempts the optional `mcp`
import lazily, inside the method body, so `import tollgate` never needs it.

Tool arguments are forwarded as `interceptor.acall(name, fn, args={...})`
rather than as `**arguments`, so a tool declaring an argument named
`session_id` or `domain` reaches the tool intact instead of being swallowed by
the interceptor's own parameters of those names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tollgate.adapters.base import AgentAdapter
from tollgate.decisions import GuardBlocked
from tollgate.errors import AdapterError, ConfigurationError

if TYPE_CHECKING:
    from tollgate.core.interceptor import TollgateInterceptor

#: Marks an object this adapter has already wrapped, so a second
#: `interceptor.use(session)` doesn't stack a second layer of policy evaluation
#: (which would double-count every call against a rate limit).
_GUARDED = "__tollgate_mcp_guarded__"


def guard_mcp_session(session: Any, interceptor: TollgateInterceptor) -> Any:
    """Route a `ClientSession`'s `tools/call` requests through `interceptor`.

    Replaces `call_tool` on the instance, so every later
    `await session.call_tool(name, arguments)` is evaluated first. A blocked
    call raises `GuardBlocked` and never reaches the server.

        async with ClientSession(read, write) as session:
            await session.initialize()
            tollgate.wrap(session, interceptor)
            await session.call_tool("delete_file", {"path": "/etc/passwd"})  # GuardBlocked
    """
    if getattr(session, _GUARDED, False):
        return session

    original_call_tool = session.call_tool

    async def guarded_call_tool(name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        # `inner` ignores the kwargs the engine passes back: the original
        # signature takes `arguments` as one dict, and a policy mutating
        # `ctx.args` is not a supported operation anyway.
        async def inner(**_policy_args: Any) -> Any:
            return await original_call_tool(name, arguments, **kwargs)

        return await interceptor.acall(name, inner, args=arguments or {})

    session.call_tool = guarded_call_tool
    setattr(session, _GUARDED, True)
    return session


def _blocked_result(exc: GuardBlocked) -> Any:
    """The MCP-native way to say "this call was refused"."""
    import mcp.types as types

    return types.ServerResult(
        types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Blocked by policy: {exc.decision.reason}")],
            isError=True,
        )
    )


def _low_level_server(server: Any) -> Any:
    """The `mcp.server.lowlevel.Server` behind `server`.

    `FastMCP` composes one rather than subclassing it, and exposes it as
    `_mcp_server` — private by name, but it is the only handle onto the
    request-handler table, and FastMCP is the shape most servers are written
    in. Falls back to `server` itself for a low-level server.
    """
    return getattr(server, "_mcp_server", server)


def guard_mcp_server(server: Any, interceptor: TollgateInterceptor) -> Any:
    """Route a `Server`'s (or `FastMCP`'s) incoming `tools/call` through `interceptor`.

    Wraps whatever handler is already registered, so call this *after* the
    server's tools are defined:

        mcp = FastMCP("my-server")

        @mcp.tool()
        def delete_file(path: str) -> str: ...

        tollgate.wrap(mcp, interceptor)

    A blocked call returns `CallToolResult(isError=True)` rather than raising —
    see the module docstring.
    """
    import mcp.types as types

    low_level = _low_level_server(server)
    if getattr(low_level, _GUARDED, False):
        return server

    handlers = getattr(low_level, "request_handlers", None)
    if handlers is None:
        raise AdapterError(f"{server!r} has no MCP request_handlers table to guard")

    original_handler = handlers.get(types.CallToolRequest)
    if original_handler is None:
        # A sequencing mistake, not a wrong-type one: the object is a server,
        # it just has nothing to guard yet.
        raise ConfigurationError(
            "no tools/call handler is registered yet — define the server's tools "
            "before wrapping it, otherwise there is nothing to guard"
        )

    async def guarded_handler(req: Any) -> Any:
        arguments = req.params.arguments or {}

        async def inner(**_policy_args: Any) -> Any:
            return await original_handler(req)

        try:
            return await interceptor.acall(req.params.name, inner, args=arguments)
        except GuardBlocked as exc:
            return _blocked_result(exc)

    handlers[types.CallToolRequest] = guarded_handler
    setattr(low_level, _GUARDED, True)
    return server


def _is_client_session(agent: Any) -> bool:
    try:
        from mcp import ClientSession
    except ImportError:
        return False
    return isinstance(agent, ClientSession)


def _is_mcp_server(agent: Any) -> bool:
    try:
        import mcp.types as types
        from mcp.server.lowlevel import Server
    except ImportError:
        return False
    low_level = _low_level_server(agent)
    if not isinstance(low_level, Server):
        return False
    return types.CallToolRequest in getattr(low_level, "request_handlers", {})


class MCPAdapter(AgentAdapter):
    """Dispatches `interceptor.use(...)` to the client- or server-side wrapper."""

    def applies_to(self, agent: Any) -> bool:
        return _is_client_session(agent) or _is_mcp_server(agent)

    def install(self, agent: Any, interceptor: TollgateInterceptor) -> Any:
        if _is_client_session(agent):
            return guard_mcp_session(agent, interceptor)
        return guard_mcp_server(agent, interceptor)
