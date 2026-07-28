"""Metric emission for each Tollgate decision.

Implements the per-event OTEL instruments: `decisions_total`,
`policy_latency_ms`, `undo_total`, `delegation_depth`,
`cross_agent_blocks_total`, `escalations_total` and
`escalation_latency_ms`. The window-aggregated gauges (`block_rate`,
`escalate_rate`, `coverage_ratio`) are computed on demand from the ledger by
`tollgate.report.policy_report` instead of pushed as live OTEL gauges — sliding
window observation is operational tooling deferred past v1 (see CLAUDE.md).

No-ops whenever OTEL isn't configured or unavailable.
"""

from __future__ import annotations

import threading
from typing import Any, Literal

from tollgate.decisions import BLOCK, GuardDecision
from tollgate.otel.config import current_settings, otel_available

_instruments: dict[str, Any] = {}
#: Two threads emitting the same metric for the first time would otherwise both
#: see a cache miss and each create an instrument, so one of the two would be
#: written to and then immediately discarded.
_instruments_lock = threading.Lock()


def reset_instruments() -> None:
    """Drop the cached instruments so the next emission rebuilds them.

    Called by `reset_otel()`: the cache binds instruments to the meter provider
    that was active when they were created, so without this a test that swaps
    providers keeps writing to the dead one.
    """
    with _instruments_lock:
        _instruments.clear()


def _meter() -> Any | None:
    settings = current_settings()
    if not (settings.enabled and otel_available()):
        return None
    from opentelemetry import metrics

    return metrics.get_meter("tollgate")


def _counter(name: str, meter: Any) -> Any:
    key = f"counter:{name}"
    with _instruments_lock:
        if key not in _instruments:
            _instruments[key] = meter.create_counter(name)
        return _instruments[key]


def _histogram(name: str, meter: Any) -> Any:
    key = f"histogram:{name}"
    with _instruments_lock:
        if key not in _instruments:
            _instruments[key] = meter.create_histogram(name, unit="ms")
        return _instruments[key]


def record_decision(
    decision: GuardDecision,
    tool_name: str,
    latency_ms: float,
    cross_agent: bool,
) -> None:
    """Record `decisions_total`, `policy_latency_ms`, and (when applicable)
    `undo_total` / `cross_agent_blocks_total` for one evaluated rule."""
    meter = _meter()
    if meter is None:
        return
    attrs = {"policy": decision.policy_name or "", "action": decision.action.value, "tool": tool_name}
    _counter("tollgate.decisions_total", meter).add(1, attrs)
    _histogram("tollgate.policy_latency_ms", meter).record(latency_ms, attrs)
    if decision.undo_executed:
        _counter("tollgate.undo_total", meter).add(1, attrs)
    if cross_agent and decision.action is BLOCK:
        _counter("tollgate.cross_agent_blocks_total", meter).add(1, attrs)


#: How an escalation ended. `not_resolved` means the engine deliberately
#: skipped the handler because the mode wasn't `enforce`.
EscalationOutcome = Literal["approved", "denied", "not_resolved"]


def record_escalation(
    outcome: EscalationOutcome,
    *,
    policy_name: str | None,
    tool_name: str,
    escalate_to: str | None,
    latency_ms: float,
) -> None:
    """Record `escalations_total` and `escalation_latency_ms` for one
    escalation attempt.

    An escalation is the one decision that puts a human in the request path,
    so its rate, approve/deny split and wait time are the highest-value
    operational signals Tollgate can emit — and there was previously no metric
    for any of them. `denied` covers a handler that said no *and* one that
    timed out; both are fail-safe blocks and the `reason` on the ledger event
    distinguishes them.
    """
    meter = _meter()
    if meter is None:
        return
    attrs = {
        "policy": policy_name or "",
        "tool": tool_name,
        "outcome": outcome,
        "escalate_to": escalate_to or "",
    }
    _counter("tollgate.escalations_total", meter).add(1, attrs)
    _histogram("tollgate.escalation_latency_ms", meter).record(latency_ms, attrs)


def record_delegation_depth(depth: int, *, agent_id: str | None = None) -> None:
    """Record one call's delegation depth, attributed to the calling agent."""
    meter = _meter()
    if meter is None:
        return
    _histogram("tollgate.delegation_depth", meter).record(depth, {"agent": agent_id or ""})
