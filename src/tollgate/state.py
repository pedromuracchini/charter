"""`CallState` — the per-session counters and accumulators policies can read.

Tollgate's core data model is deliberately stateless: a `GuardContext` is a
value describing one call, and a predicate is a pure function of it. That rules
out the two policies people ask for most — "block after N calls this session"
and "stop once this session has spent $X" — because both need to remember
something across calls.

This module adds that memory *beside* the context rather than inside it. A
`CallState` is a separate object, injected into the `ExecutionScope` the same
way `checksum_provider`/`consent_provider` already are, and reached through a
small read-only surface on `GuardContext` (`calls_this_session`, `spent`).
`GuardContext` itself stays a plain value, and replay stays deterministic:
`ledger.replay()` reconstructs a scope with no `CallState`, so a
history-dependent policy reports zero rather than silently re-reading today's
live counters.

Counting happens on *attempts*, not successes — a call the engine goes on to
block still increments. For a rate limit that is the useful behavior: retrying
a denied call must not be free.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

#: Distinct sessions tracked before the least-recently-used one is evicted.
#: Mirrors `TollgateInterceptor.DEFAULT_MAX_SESSIONS`: an eviction resets that
#: session's counters, which is a memory bound, not a correctness guarantee.
DEFAULT_MAX_SESSIONS = 10_000

#: Key used for a session's total call count, across every tool.
_ALL_TOOLS = "*"


class _SessionState:
    """One session's counters. Always accessed under `CallState._lock`."""

    __slots__ = ("calls", "spend")

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.spend: dict[str, float] = {}


class CallState:
    """Cross-call counters and spend accumulators, keyed by session.

    Thread-safe, and bounded by `max_sessions` (LRU) so a long-lived process
    with high session churn doesn't grow without limit. Pass
    `max_sessions=None` for unbounded tracking.
    """

    def __init__(self, max_sessions: int | None = DEFAULT_MAX_SESSIONS) -> None:
        self._sessions: OrderedDict[str, _SessionState] = OrderedDict()
        self._max_sessions = max_sessions
        self._lock = threading.Lock()

    def _touch(self, session_id: str) -> _SessionState:
        """Fetch (creating if needed) a session's state and mark it recently
        used. Caller must hold `self._lock`."""
        state = self._sessions.get(session_id)
        if state is None:
            state = _SessionState()
            self._sessions[session_id] = state
        self._sessions.move_to_end(session_id)
        if self._max_sessions is not None and len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
        return state

    def record_call(self, session_id: str, tool_name: str) -> None:
        """Count one attempted call. Invoked by the engine before any policy
        runs, so a rule can see the call it is currently deciding on."""
        with self._lock:
            state = self._touch(session_id)
            state.calls[tool_name] = state.calls.get(tool_name, 0) + 1
            state.calls[_ALL_TOOLS] = state.calls.get(_ALL_TOOLS, 0) + 1

    def call_count(self, session_id: str, tool_name: str | None = None) -> int:
        """Calls attempted in this session — for `tool_name`, or every tool."""
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                return 0
            return state.calls.get(tool_name if tool_name is not None else _ALL_TOOLS, 0)

    def add_spend(self, session_id: str, key: str, amount: float) -> float:
        """Add `amount` to the named accumulator, returning the new total."""
        with self._lock:
            state = self._touch(session_id)
            state.spend[key] = state.spend.get(key, 0.0) + amount
            return state.spend[key]

    def spend(self, session_id: str, key: str) -> float:
        """The named accumulator's current total for this session."""
        with self._lock:
            state = self._sessions.get(session_id)
            return 0.0 if state is None else state.spend.get(key, 0.0)

    def reset_session(self, session_id: str) -> None:
        """Forget one session's counters — e.g. when a conversation ends."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear(self) -> None:
        """Forget every session."""
        with self._lock:
            self._sessions.clear()
