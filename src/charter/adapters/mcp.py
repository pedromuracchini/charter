"""Model Context Protocol adapter — guards `tools/call` on either side.

MCP is how most agents reach tools today, and it has two very different
vantage points. Charter supports both, because they answer different
questions:

- **Client side** (`guard_mcp_session`) — you run the agent and want to police
  what it asks *any* server to do. Wraps `ClientSession.call_tool`.
- **Server side** (`guard_mcp_server`) — you run the MCP server and want the
  policy enforced no matter which client connects. Wraps whatever handler is
  registered for `tools/call`.

The two differ in how a denial is reported, and the difference is not
cosmetic. A client-side block raises `GuardBlocked` into the caller that
asked for the tool — that code is yours, and an exception is the honest
answer. A server-side block must **not** raise: an exception escaping a
request handler tears down the protocol connection for every subsequent
request. It returns a `CallToolResult(isError=True)` instead, which is exactly
what MCP defines for "this tool call failed" and which the model can read and
react to.

## Both mcp 1.x and 2.x, detected rather than configured

mcp 2.0 rewrote the server-side registration API wholesale, and this adapter
reaches straight into it, so it detects which generation it is talking to
instead of asking the caller. The four things that moved:

===================  ==========================================  =========================================
                     mcp 1.x                                     mcp 2.x
===================  ==========================================  =========================================
server class         `FastMCP`                                   `MCPServer`
low-level handle     `server._mcp_server`                        `server._lowlevel_server`
handler table        `Server.request_handlers[CallToolRequest]`  `Server.get_request_handler("tools/call")`
                     (keyed by request *type*)                   `Server.add_request_handler(...)`
                                                                 (keyed by method *string*)
handler contract     `(req) -> ServerResult(CallToolResult(…))`  `(ctx, params) -> CallToolResult(…)`
===================  ==========================================  =========================================

`_server_generation()` picks between them on the presence of
`add_request_handler`, which exists only on 2.x. Both handles are private by
name; 2.x's own in-memory transport reaches for `_lowlevel_server` with a
`TODO: make it public` beside it, so this is the supported-in-practice seam
rather than a shortcut.

The client side needed no such split — `ClientSession.call_tool(name,
arguments, ...)` kept its leading parameters — but 2.x adds a `Client` facade
over the session, and that is what its own documentation hands you. Wrapping
the session covers both, because `Client.call_tool` delegates to
`self.session.call_tool` on every call rather than binding it once.

Install the extra to use this: `uv sync --extra mcp`. Registered by default
(see `charter.adapters`); `applies_to()` only attempts the optional `mcp`
import lazily, inside the method body, so `import charter` never needs it.

Tool arguments are forwarded as `interceptor.acall(name, fn, args={...})`
rather than as `**arguments`, so a tool declaring an argument named
`session_id` or `domain` reaches the tool intact instead of being swallowed by
the interceptor's own parameters of those names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from charter.adapters.base import AgentAdapter
from charter.decisions import GuardBlocked
from charter.errors import AdapterError, ConfigurationError

if TYPE_CHECKING:
    from charter.core.interceptor import CharterInterceptor

#: Marks an object this adapter has already wrapped, so a second
#: `interceptor.use(session)` doesn't stack a second layer of policy evaluation
#: (which would double-count every call against a rate limit).
_GUARDED = "__charter_mcp_guarded__"

#: The JSON-RPC method mcp 2.x keys its handler table by. 1.x keys the same
#: handler by the `types.CallToolRequest` class instead.
_CALL_TOOL_METHOD = "tools/call"


# --- client side -----------------------------------------------------------


def _client_classes() -> tuple[Any, Any]:
    """`(ClientSession, Client)` from the installed mcp, either possibly `None`.

    `Client` is 2.x-only, so a missing one is a version signal rather than a
    broken install.
    """
    try:
        from mcp import ClientSession
    except ImportError:
        return None, None
    client_cls: Any = None
    try:
        from mcp.client import Client
    except ImportError:
        pass
    else:
        client_cls = Client
    return ClientSession, client_cls


def _is_client_like(agent: Any) -> bool:
    """Whether `agent` is a `ClientSession` or a 2.x `Client` wrapping one.

    Deliberately isinstance-only: this runs from `applies_to()`, where reading
    `Client.session` would raise on a client that has not connected yet, and
    an adapter probe must never raise for an object it simply does not claim.
    """
    session_cls, client_cls = _client_classes()
    if session_cls is None:
        return False
    if isinstance(agent, session_cls):
        return True
    return client_cls is not None and isinstance(agent, client_cls)


def _unwrap_session(agent: Any) -> Any:
    """The `ClientSession` to guard, given a session or a 2.x `Client`."""
    session_cls, client_cls = _client_classes()
    if session_cls is not None and isinstance(agent, session_cls):
        return agent
    if client_cls is not None and isinstance(agent, client_cls):
        try:
            return agent.session
        except RuntimeError as exc:
            # `Client.session` is only populated after the handshake, so this
            # is a sequencing mistake with a precise fix.
            raise ConfigurationError(
                "this MCP Client has no session yet — wrap it inside its "
                "`async with Client(...) as client:` block, after the connection "
                "is established"
            ) from exc
    raise AdapterError(f"{agent!r} is not an MCP ClientSession or Client")


def guard_mcp_session(session: Any, interceptor: CharterInterceptor) -> Any:
    """Route a `ClientSession`'s `tools/call` requests through `interceptor`.

    Replaces `call_tool` on the instance, so every later
    `await session.call_tool(name, arguments)` is evaluated first. A blocked
    call raises `GuardBlocked` and never reaches the server.

        async with ClientSession(read, write) as session:
            await session.initialize()
            charter.wrap(session, interceptor)
            await session.call_tool("delete_file", {"path": "/etc/passwd"})  # GuardBlocked

    Accepts an mcp 2.x `Client` too, and guards the session underneath it — so
    the facade's own `call_tool` is covered as well. Returns whatever it was
    given, so `wrap(client, ...) is client` holds.
    """
    target = _unwrap_session(session)
    if getattr(target, _GUARDED, False):
        return session

    original_call_tool = target.call_tool

    async def guarded_call_tool(name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        # `inner` ignores the kwargs the engine passes back: the original
        # signature takes `arguments` as one dict, and a policy mutating
        # `ctx.args` is not a supported operation anyway.
        async def inner(**_policy_args: Any) -> Any:
            return await original_call_tool(name, arguments, **kwargs)

        return await interceptor.acall(name, inner, args=arguments or {})

    target.call_tool = guarded_call_tool
    setattr(target, _GUARDED, True)
    return session


# --- server side -----------------------------------------------------------


def _blocked_call_tool_result(exc: GuardBlocked) -> Any:
    """The MCP-native way to say "this call was refused"."""
    import mcp.types as types

    # 1.x declares the field as `isError`; 2.x renamed it to `is_error` and
    # kept `isError` only as a pydantic alias. Passing the alias happens to
    # work today, but populate-by-alias is a model config away from changing,
    # so name the field the installed version actually declares.
    error_field = "is_error" if "is_error" in types.CallToolResult.model_fields else "isError"
    payload: dict[str, Any] = {
        "content": [types.TextContent(type="text", text=f"Blocked by policy: {exc.decision.reason}")],
        error_field: True,
    }
    return types.CallToolResult.model_validate(payload)


def _low_level_server(server: Any) -> Any:
    """The `mcp.server.lowlevel.Server` behind `server`.

    Neither `FastMCP` (1.x) nor `MCPServer` (2.x) subclasses it — both compose
    one and expose it under a private name, and that name changed between the
    two. Falls back to `server` itself for a low-level server passed directly.
    """
    for attr in ("_lowlevel_server", "_mcp_server"):
        low_level = getattr(server, attr, None)
        if low_level is not None:
            return low_level
    return server


def _uses_method_handlers(low_level: Any) -> bool:
    """Whether this is mcp 2.x, whose handler table is keyed by method string.

    `add_request_handler` is the discriminator: 2.x made the table private and
    put these accessors in front of it, and 1.x has neither.
    """
    return hasattr(low_level, "add_request_handler")


def _guard_method_keyed(low_level: Any, interceptor: CharterInterceptor) -> None:
    """mcp 2.x: `(ctx, params) -> CallToolResult`, registered by method string."""
    entry = low_level.get_request_handler(_CALL_TOOL_METHOD)
    if entry is None:
        raise ConfigurationError(
            "no tools/call handler is registered yet — define the server's tools "
            "before wrapping it, otherwise there is nothing to guard"
        )
    original_handler = entry.handler

    async def guarded_handler(ctx: Any, params: Any) -> Any:
        async def inner(**_policy_args: Any) -> Any:
            return await original_handler(ctx, params)

        try:
            return await interceptor.acall(params.name, inner, args=params.arguments or {})
        except GuardBlocked as exc:
            return _blocked_call_tool_result(exc)

    # Re-registering the same method replaces the entry, keeping the params
    # model the server validated against so the wrapper is invisible to the
    # runner's validation step.
    low_level.add_request_handler(_CALL_TOOL_METHOD, entry.params_type, guarded_handler)


def _guard_type_keyed(low_level: Any, interceptor: CharterInterceptor) -> None:
    """mcp 1.x: `(req) -> ServerResult(CallToolResult)`, registered by request type."""
    import mcp.types as types

    handlers = low_level.request_handlers
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
            # 1.x handlers return the wire envelope; 2.x returns the bare
            # result (and turns `ServerResult` into a non-callable union), so
            # this line is unreachable there.
            envelope: Any = types.ServerResult
            return envelope(_blocked_call_tool_result(exc))

    handlers[types.CallToolRequest] = guarded_handler


def guard_mcp_server(server: Any, interceptor: CharterInterceptor) -> Any:
    """Route a `Server`'s (or `FastMCP`'s/`MCPServer`'s) `tools/call` through `interceptor`.

    Wraps whatever handler is already registered, so call this *after* the
    server's tools are defined:

        server = MCPServer("my-server")   # or FastMCP("my-server") on mcp 1.x

        @server.tool()
        def delete_file(path: str) -> str: ...

        charter.wrap(server, interceptor)

    A blocked call returns `CallToolResult(isError=True)` rather than raising —
    see the module docstring.
    """
    low_level = _low_level_server(server)
    if getattr(low_level, _GUARDED, False):
        return server

    if _uses_method_handlers(low_level):
        _guard_method_keyed(low_level, interceptor)
    elif hasattr(low_level, "request_handlers"):
        _guard_type_keyed(low_level, interceptor)
    else:
        raise AdapterError(f"{server!r} has no MCP request-handler table to guard")

    setattr(low_level, _GUARDED, True)
    return server


def _is_mcp_server(agent: Any) -> bool:
    try:
        from mcp.server.lowlevel import Server
    except ImportError:
        return False

    low_level = _low_level_server(agent)
    if not isinstance(low_level, Server):
        return False

    if _uses_method_handlers(low_level):
        return low_level.get_request_handler(_CALL_TOOL_METHOD) is not None

    import mcp.types as types

    return types.CallToolRequest in getattr(low_level, "request_handlers", {})


class MCPAdapter(AgentAdapter):
    """Dispatches `interceptor.use(...)` to the client- or server-side wrapper."""

    def applies_to(self, agent: Any) -> bool:
        return _is_client_like(agent) or _is_mcp_server(agent)

    def install(self, agent: Any, interceptor: CharterInterceptor) -> Any:
        if _is_client_like(agent):
            return guard_mcp_session(agent, interceptor)
        return guard_mcp_server(agent, interceptor)
