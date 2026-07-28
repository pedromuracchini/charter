"""Span emission for each Tollgate evaluation.

Emits a `tollgate.evaluate` child span per rule evaluation, carrying the
policy/decision/caller attributes described in `CLAUDE.md`. No-ops (yields no
trace/span ids) whenever OTEL isn't available, no tracer provider is
reachable, or the event is sampled out.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from tollgate.core.context import GuardContext
from tollgate.decisions import BLOCK, GuardDecision
from tollgate.otel.config import current_settings, otel_available


@contextmanager
def evaluate_span(
    ctx: GuardContext,
    decision: GuardDecision,
    hook: str,
    dry_run: bool,
    latency_ms: float,
    tracer_provider: Any = None,
) -> Iterator[tuple[str, str] | None]:
    """Emit a `tollgate.evaluate` span for one rule evaluation.

    `tracer_provider`, if given (typically `TollgateInterceptor.otel_tracer`),
    takes precedence over the tracer provider set globally via
    `configure_otel()` — letting one interceptor use its own tracer without
    affecting others. Sample rates always come from the global settings.

    Yields `(trace_id_hex, span_id_hex)` if a span was actually started (so the
    ledger event can be correlated to it), or `None` if OTEL is unavailable, no
    tracer provider is reachable, or this event was sampled out.
    """
    settings = current_settings()
    provider = tracer_provider or settings.tracer_provider
    if not (otel_available() and provider is not None):
        yield None
        return

    sample_rate = settings.block_sample_rate if decision.action is BLOCK else settings.allow_sample_rate
    if random.random() > sample_rate:
        yield None
        return

    from opentelemetry import trace

    tracer = trace.get_tracer("tollgate", tracer_provider=provider)
    with tracer.start_as_current_span("tollgate.evaluate") as span:
        span.set_attribute("tollgate.policy", decision.policy_name or "")
        span.set_attribute("tollgate.policy_hash", decision.policy_hash or "")
        span.set_attribute("tollgate.action", decision.action.value)
        span.set_attribute("tollgate.hook", hook)
        span.set_attribute("tollgate.severity", decision.severity)
        span.set_attribute("tollgate.reason", decision.reason)
        span.set_attribute("tollgate.latency_ms", latency_ms)
        span.set_attribute("tollgate.caller_agent_id", ctx.caller_agent_id or "")
        span.set_attribute("tollgate.caller_role", ctx.caller_role or "")
        span.set_attribute("tollgate.delegation_chain", "→".join(ctx.delegation_chain))
        span.set_attribute("tollgate.dry_run", dry_run)

        span_context = span.get_span_context()
        yield (
            trace.format_trace_id(span_context.trace_id),
            trace.format_span_id(span_context.span_id),
        )
