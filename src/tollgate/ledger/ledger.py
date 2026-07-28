"""`ActionLedger` — the append-only, in-process record of every decision.

A process-wide instance is reachable via `ActionLedger.current()`. In-memory
storage is a bounded ring buffer (`max_events`, default 10,000) — a memory
bound, not a durability story: full lossless history requires `sink_path`, a
JSONL file every event is mirrored to regardless of the in-memory cap.
"""

from __future__ import annotations

import csv
import io
import json
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Literal

from tollgate.core.context import GuardContext
from tollgate.core.policy_set import Policy
from tollgate.decisions import RuleResult, pick_decision
from tollgate.ledger.event import LedgerEvent

ComplianceFormat = Literal["json", "csv"]
GraphFormat = Literal["dot", "mermaid", "json"]
NarrativeAudience = Literal["technical", "non-technical"]

DEFAULT_MAX_EVENTS = 10_000


class ActionLedger:
    """Append-only store of `LedgerEvent`s for the current process.

    In-memory storage is capped at `max_events` (default `DEFAULT_MAX_EVENTS`)
    — once full, the oldest event is evicted to make room for each new one.
    Pass `max_events=None` for unbounded in-memory storage (the old default).
    """

    _singleton: ActionLedger | None = None

    def __init__(
        self,
        sink_path: str | PathLike[str] | None = None,
        max_events: int | None = DEFAULT_MAX_EVENTS,
    ) -> None:
        self._lock = threading.Lock()
        self._events: deque[LedgerEvent] = deque(maxlen=max_events)
        self._sink_path = Path(sink_path) if sink_path is not None else None
        self._dropped_count = 0

    @classmethod
    def current(cls) -> ActionLedger:
        """The process-wide `ActionLedger` singleton."""
        if cls._singleton is None:
            cls._singleton = cls()
        return cls._singleton

    @classmethod
    def reset(cls) -> None:
        """Replace the process-wide singleton with a fresh, empty ledger.

        Intended for tests; production code should not need this.
        """
        cls._singleton = cls()

    @property
    def dropped_count(self) -> int:
        """How many events have been evicted from memory because `max_events`
        was reached. `sink_path`, if configured, still has all of them."""
        with self._lock:
            return self._dropped_count

    def record(self, event: LedgerEvent) -> LedgerEvent:
        with self._lock:
            if self._events.maxlen is not None and len(self._events) == self._events.maxlen:
                self._dropped_count += 1
            self._events.append(event)
            if self._sink_path is not None:
                with self._sink_path.open("a", encoding="utf-8") as f:
                    f.write(event.model_dump_json() + "\n")
        return event

    def events(self) -> list[LedgerEvent]:
        """A snapshot list of every event recorded so far, oldest first."""
        with self._lock:
            return list(self._events)

    def get(self, event_id: str) -> LedgerEvent | None:
        for event in self.events():
            if event.event_id == event_id:
                return event
        return None

    def export_compliance_report(self, format: ComplianceFormat = "json") -> str:
        events = self.events()
        if format == "json":
            return json.dumps([e.model_dump() for e in events], indent=2, default=str)
        if format == "csv":
            return _events_to_csv(events)
        raise ValueError(f"unsupported compliance report format: {format!r}")

    def export_policy_graph(self, format: GraphFormat = "mermaid") -> str:
        from tollgate.report.graph import policy_graph

        return policy_graph(self.events(), format=format)

    def export_delegation_graph(self, format: Literal["dot", "mermaid"] = "mermaid") -> str:
        from tollgate.report.graph import delegation_graph

        return delegation_graph(self.events(), format=format)

    def export_narrative(self, audience: NarrativeAudience = "non-technical") -> str:
        from tollgate.report.narrative import narrative

        return narrative(self.events(), audience=audience)

    def export_fixtures(self, framework: Literal["pytest"] = "pytest") -> str:
        from tollgate.testing.harness import fixtures_from_events

        return fixtures_from_events(self.events(), framework=framework)


def _events_to_csv(events: Iterable[LedgerEvent]) -> str:
    events = list(events)
    buffer = io.StringIO()
    if not events:
        return ""
    fieldnames = list(events[0].model_dump().keys())
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for event in events:
        row = event.model_dump()
        row["args"] = json.dumps(row["args"])
        row["delegation_chain"] = "|".join(row["delegation_chain"])
        writer.writerow(row)
    return buffer.getvalue()


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of replaying a past ledger event against a set of policies."""

    event: LedgerEvent
    context: GuardContext
    original_decision: str
    new_results: tuple[RuleResult, ...] | None
    changed: bool


def replay(
    event_id: str,
    policies: Iterable[Policy] | None = None,
    hook: Literal["pre", "post"] = "pre",
) -> ReplayResult:
    """Reconstruct the exact `GuardContext` for a past event and, if `policies`
    are given, re-evaluate them against it — useful for confirming whether a
    policy change would have produced a different decision.

    Without `policies`, only the reconstructed context and the event itself are
    returned (`new_results=None`, `changed=False`).
    """
    event = ActionLedger.current().get(event_id)
    if event is None:
        raise KeyError(f"no ledger event found with id {event_id!r}")

    from tollgate._scope import ExecutionScope

    scope = ExecutionScope(
        session_id=event.session_id,
        step_index=event.step_index,
        caller_agent_id=event.caller_agent_id,
        caller_role=event.caller_role,
        delegation_chain=tuple(event.delegation_chain),
        trust_level=event.trust_level,
        state_checksum=event.checksum_expected,
    )
    ctx = GuardContext.build(tool_name=event.tool, args=event.args, scope=scope)

    if policies is None:
        return ReplayResult(
            event=event, context=ctx, original_decision=event.decision, new_results=None, changed=False
        )

    results: list[RuleResult] = []
    for policy in policies:
        results.extend(policy.evaluate(ctx, hook))
    failing = [r for r in results if not r.passed]
    worst = pick_decision(failing)
    new_decision = worst.on_fail.value.upper() if worst is not None else "ALLOW"
    return ReplayResult(
        event=event,
        context=ctx,
        original_decision=event.decision,
        new_results=tuple(results),
        changed=new_decision != event.decision,
    )
