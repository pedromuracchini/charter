"""Metric emission for each Tollgate decision.

Implements the per-event OTEL instruments: `decisions_total`,
`policy_latency_ms`, `undo_total`, `delegation_depth`, and
`cross_agent_blocks_total`. The window-aggregated gauges (`block_rate`,
`escalate_rate`, `coverage_ratio`) are computed on demand from the ledger by
`tollgate.report.policy_report` instead of pushed as live OTEL gauges — sliding
window observation is operational tooling deferred past v1 (see CLAUDE.md).

No-ops whenever OTEL isn't configured or unavailable.
"""

from __future__ import annotations

from typing import Any

from tollgate.decisions import BLOCK, GuardDecision
from tollgate.otel.config import current_settings, otel_available

_instruments: dict[str, Any] = {}


def _meter() -> Any | None:
    settings = current_settings()
    if not (settings.enabled and otel_available()):
        return None
    from opentelemetry import metrics

    return metrics.get_meter("tollgate")


def _counter(name: str, meter: Any) -> Any:
    key = f"counter:{name}"
    if key not in _instruments:
        _instruments[key] = meter.create_counter(name)
    return _instruments[key]


def _histogram(name: str, meter: Any) -> Any:
    key = f"histogram:{name}"
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


def record_delegation_depth(depth: int) -> None:
    meter = _meter()
    if meter is None:
        return
    _histogram("tollgate.delegation_depth", meter).record(depth)
