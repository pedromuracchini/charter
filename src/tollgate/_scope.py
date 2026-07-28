"""Ambient per-call execution context propagated via `contextvars`.

`@guard`-decorated functions don't receive a `ctx` argument from the caller, so
session and identity data that isn't part of the tool's own arguments has to come
from somewhere else. `ExecutionScope` is that somewhere: `TollgateInterceptor` (or
the `session()` context manager, for bare `@guard` usage outside an interceptor)
installs it before a tool call, and the engine reads it when building a
`GuardContext`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class ExecutionScope:
    """Ambient data merged into every `GuardContext` built while it is active."""

    session_id: str = "default"
    step_index: int = 0
    domain: str | None = None
    caller_agent_id: str | None = None
    caller_role: str | None = None
    delegation_chain: tuple[str, ...] = field(default_factory=tuple)
    trust_level: int = 0
    state_checksum: str | None = None
    checksum_provider: Callable[[], str] | None = None
    consent_provider: Callable[[str], bool] | None = None


_ROOT_SCOPE = ExecutionScope()
_current_scope: ContextVar[ExecutionScope] = ContextVar("tollgate_scope", default=_ROOT_SCOPE)


def current_scope() -> ExecutionScope:
    """The `ExecutionScope` active for the calling task/thread right now."""
    return _current_scope.get()


@contextmanager
def use_scope(scope: ExecutionScope) -> Iterator[ExecutionScope]:
    """Install `scope` verbatim as the active `ExecutionScope` for a `with` block.

    Used by `TollgateInterceptor` so that any `@guard`-decorated helper called
    from within an intercepted tool inherits the same caller identity.
    """
    token = _current_scope.set(scope)
    try:
        yield scope
    finally:
        _current_scope.reset(token)


@contextmanager
def session(**overrides: Any) -> Iterator[ExecutionScope]:
    """Install an `ExecutionScope` derived from the current one for a `with` block.

    Only the fields passed as keyword arguments are overridden; everything else
    is inherited from whatever scope was already active (so nested `session()`
    calls compose). For bare `@guard()` usage outside of any
    `TollgateInterceptor` — the interceptor manages scope on its own.
    """
    with use_scope(replace(current_scope(), **overrides)) as scope:
        yield scope
