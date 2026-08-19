"""`ActionLedger` — the append-only, in-process record of every decision.

A process-wide instance is reachable via `ActionLedger.current()`. In-memory
storage is a bounded ring buffer (`max_events`, default 10,000) — a memory
bound, not a durability story: full lossless history requires `sink_path`, a
JSONL file every event is mirrored to regardless of the in-memory cap.

Configure the process-wide ledger once, before any guarded call, with
`ActionLedger.configure(sink_path=...)` (exported as `chokepoint.configure_ledger`).
`current()` lazily builds an unconfigured default if nothing did.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Literal

from chokepoint.core.context import GuardContext
from chokepoint.core.policy_set import Policy
from chokepoint.decisions import RuleResult, pick_decision
from chokepoint.errors import ConfigurationError, LedgerEventNotFound
from chokepoint.ledger.event import LedgerEvent
from chokepoint.redaction import contains_placeholder

logger = logging.getLogger("chokepoint.ledger")

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
    #: Guards singleton creation/replacement. Distinct from each instance's own
    #: `_lock`, which guards that instance's events.
    _singleton_lock: threading.Lock = threading.Lock()

    def __init__(
        self,
        sink_path: str | PathLike[str] | None = None,
        max_events: int | None = DEFAULT_MAX_EVENTS,
    ) -> None:
        self._lock = threading.Lock()
        self._events: deque[LedgerEvent] = deque(maxlen=max_events)
        self._sink_path = Path(sink_path) if sink_path is not None else None
        self._dropped_count = 0
        self._sink_error_count = 0

    def __repr__(self) -> str:
        with self._lock:
            count, dropped, errors = len(self._events), self._dropped_count, self._sink_error_count
        parts = [f"events={count}", f"dropped={dropped}"]
        if self._sink_path is not None:
            parts.append(f"sink_path={str(self._sink_path)!r}")
        if errors:
            parts.append(f"sink_errors={errors}")
        return f"<ActionLedger {' '.join(parts)}>"

    @property
    def sink_path(self) -> Path | None:
        """The JSONL file every event is mirrored to, if one was configured."""
        return self._sink_path

    @classmethod
    def current(cls) -> ActionLedger:
        """The process-wide `ActionLedger` singleton.

        Lazily creates an unconfigured default. Use `configure()` before the
        first guarded call to set `sink_path`/`max_events` instead.
        """
        if cls._singleton is None:
            with cls._singleton_lock:
                # Re-check: another thread may have created it while we waited.
                if cls._singleton is None:
                    cls._singleton = cls()
        return cls._singleton

    @classmethod
    def configure(
        cls,
        *,
        sink_path: str | PathLike[str] | None = None,
        max_events: int | None = DEFAULT_MAX_EVENTS,
    ) -> ActionLedger:
        """Install a freshly configured process-wide ledger, and return it.

        This is the only supported way to enable `sink_path` for the ledger the
        engine actually writes to: `current()` builds its lazy default with no
        arguments, so a `ActionLedger(sink_path=...)` constructed by hand is
        never consulted by anything.

        Call once at startup, before the first guarded call — any events already
        recorded stay with the old ledger and are **not** migrated.
        """
        with cls._singleton_lock:
            cls._singleton = cls(sink_path=sink_path, max_events=max_events)
            return cls._singleton

    @classmethod
    def reset(cls) -> None:
        """Replace the process-wide singleton with a fresh, empty ledger.

        Drops any `sink_path`/`max_events` set via `configure()`. Intended for
        tests; production code should not need this.
        """
        with cls._singleton_lock:
            cls._singleton = cls()

    @property
    def dropped_count(self) -> int:
        """How many events have been evicted from memory because `max_events`
        was reached. `sink_path`, if configured, still has all of them."""
        with self._lock:
            return self._dropped_count

    @property
    def sink_error_count(self) -> int:
        """How many events failed to reach `sink_path`.

        Non-zero means the on-disk audit trail has gaps the in-memory ledger
        does not — worth alerting on, since `record()` deliberately swallows
        these rather than failing the guarded call.
        """
        with self._lock:
            return self._sink_error_count

    def record(self, event: LedgerEvent) -> LedgerEvent:
        """Append one event to memory and, if configured, to the JSONL sink.

        A sink write that fails is logged and counted in `sink_error_count`,
        never raised. `record()` runs *after* the guarded tool has already
        executed, so propagating an `OSError` from a full disk or a read-only
        path would convert a call the policies explicitly allowed into a
        failure — losing the tool's result to protect a copy of the record that
        is also in memory. The in-memory event is appended first, so it
        survives regardless.

        The file is opened and closed per event rather than held open, which
        keeps the sink safe to rotate underneath a long-lived process. The
        write happens under the instance lock so concurrent recorders can't
        interleave partial lines into the file.
        """
        with self._lock:
            if self._events.maxlen is not None and len(self._events) == self._events.maxlen:
                self._dropped_count += 1
            self._events.append(event)
            if self._sink_path is not None:
                try:
                    with self._sink_path.open("a", encoding="utf-8") as f:
                        f.write(event.model_dump_json() + "\n")
                except OSError as exc:
                    self._sink_error_count += 1
                    logger.error(
                        "could not write event %s to ledger sink %s: %s: %s — "
                        "the event is still in the in-memory ledger",
                        event.event_id,
                        self._sink_path,
                        type(exc).__name__,
                        exc,
                    )
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
        raise ConfigurationError(f"unsupported compliance report format: {format!r}")

    def export_policy_graph(self, format: GraphFormat = "mermaid") -> str:
        from chokepoint.report.graph import policy_graph

        return policy_graph(self.events(), format=format)

    def export_delegation_graph(self, format: Literal["dot", "mermaid"] = "mermaid") -> str:
        from chokepoint.report.graph import delegation_graph

        return delegation_graph(self.events(), format=format)

    def export_narrative(self, audience: NarrativeAudience = "non-technical") -> str:
        from chokepoint.report.narrative import narrative

        return narrative(self.events(), audience=audience)

    def export_fixtures(self, framework: Literal["pytest"] = "pytest") -> str:
        from chokepoint.testing.harness import fixtures_from_events

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
    #: Whether the stored arguments were redacted before being written. When
    #: true, the predicates just saw placeholders rather than the values they
    #: originally decided on, so `changed` says little — treat the whole result
    #: as unreliable. See `chokepoint.redaction`.
    redacted: bool = False


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

    Two things the reconstructed context cannot restore, both of which make a
    replayed decision diverge from the original rather than merely differ:
    the scope's `checksum_provider`/`consent_provider` (so
    `state_checksum_matches()` and `patient_consent_on_file()` fail safe to
    `False`) and any `CallState`, so history-dependent policies read zero. On
    top of that, arguments that were redacted at record time come back as
    placeholders — `ReplayResult.redacted` flags that case and a warning is
    logged, because a predicate reading a redacted value is not re-deciding
    the original call in any meaningful sense.
    """
    event = ActionLedger.current().get(event_id)
    if event is None:
        raise LedgerEventNotFound(f"no ledger event found with id {event_id!r}")

    from chokepoint._scope import ExecutionScope

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
    redacted = contains_placeholder(event.args)

    if policies is None:
        return ReplayResult(
            event=event,
            context=ctx,
            original_decision=event.decision,
            new_results=None,
            changed=False,
            redacted=redacted,
        )

    if redacted:
        logger.warning(
            "event %s was recorded with redacted arguments — replaying it evaluates "
            "policies against placeholders, so the result is not comparable to the "
            "original decision",
            event_id,
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
        redacted=redacted,
    )
