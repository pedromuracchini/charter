"""Span emission for each Tollgate evaluation.

Emits a `tollgate.evaluate` child span per rule evaluation, carrying the
policy/decision/caller attributes described in `CLAUDE.md`. No-ops (yields no
trace/span ids) whenever OTEL isn't available, no tracer provider is
reachable, or the event is sampled out.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from tollgate.core.context import GuardContext
from tollgate.decisions import ALLOW, BLOCK, GuardDecision
from tollgate.otel.config import current_settings, otel_available

#: A private RNG rather than the `random` module's shared one. Sampling would
#: otherwise consume the process-wide random stream, silently changing the
#: sequence any caller who seeded `random` for reproducibility depends on.
#: Guarded by a lock because `random.Random` is not thread-safe and sampling
#: runs on every recorded event.
_rng = random.Random()
_rng_lock = threading.Lock()


def should_sample(decision: GuardDecision) -> bool:
    """Roll this decision against the configured sample rate, once.

    The caller owns the result and passes it to `evaluate_span(sampled=...)`,
    so the ledger and the span always agree. Previously `_record_allow` rolled
    at `allow_sample_rate` and `evaluate_span` rolled again at the same rate,
    making the effective span rate `allow_sample_rate²`.

    Anything that isn't an ALLOW — a BLOCK *or* an ESCALATE — is a failure and
    samples at `block_sample_rate`. ESCALATE used to fall through to
    `allow_sample_rate`, so dialing allows down silently thinned the
    approval-request spans too.

    The comparison is strict (`<`), so `rate=0.0` samples nothing: `random()`
    can return exactly 0.0, and `<=` let that one draw through a sample rate
    the caller had explicitly turned off. `rate=1.0` still always samples,
    since `random()` never returns 1.0.
    """
    settings = current_settings()
    rate = settings.allow_sample_rate if decision.action is ALLOW else settings.block_sample_rate
    with _rng_lock:
        return _rng.random() < rate


@contextmanager
def evaluate_span(
    ctx: GuardContext,
    decision: GuardDecision,
    hook: str,
    dry_run: bool,
    latency_ms: float,
    tracer_provider: Any = None,
    sampled: bool = True,
) -> Iterator[tuple[str, str] | None]:
    """Emit a `tollgate.evaluate` span for one rule evaluation.

    `tracer_provider`, if given (typically `TollgateInterceptor.otel_tracer`),
    takes precedence over the tracer provider set globally via
    `configure_otel()` — letting one interceptor use its own tracer without
    affecting others.

    `sampled` is the caller's already-made sampling decision (see
    `should_sample`); this function never rolls its own.

    Yields `(trace_id_hex, span_id_hex)` if a span was actually started (so the
    ledger event can be correlated to it), or `None` if OTEL is unavailable, no
    tracer provider is reachable, or this event was sampled out.
    """
    settings = current_settings()
    provider = tracer_provider or settings.tracer_provider
    if not (otel_available() and provider is not None and sampled):
        yield None
        return

    from opentelemetry import trace

    tracer = trace.get_tracer("tollgate", tracer_provider=provider)
    with tracer.start_as_current_span("tollgate.evaluate") as span:
        # `tollgate.tool` is what makes these spans groupable in a backend —
        # "which tool is getting blocked?" is the first question anyone asks.
        span.set_attribute("tollgate.tool", ctx.tool_name)
        span.set_attribute("tollgate.policy", decision.policy_name or "")
        span.set_attribute("tollgate.policy_hash", decision.policy_hash or "")
        span.set_attribute("tollgate.action", decision.action.value)
        span.set_attribute("tollgate.hook", hook)
        span.set_attribute("tollgate.severity", decision.severity)
        span.set_attribute("tollgate.reason", decision.reason)
        span.set_attribute("tollgate.latency_ms", latency_ms)
        span.set_attribute("tollgate.session_id", ctx.session_id)
        span.set_attribute("tollgate.step_index", ctx.step_index)
        span.set_attribute("tollgate.caller_agent_id", ctx.caller_agent_id or "")
        span.set_attribute("tollgate.caller_role", ctx.caller_role or "")
        span.set_attribute("tollgate.trust_level", ctx.trust_level)
        span.set_attribute("tollgate.delegation_chain", "→".join(ctx.delegation_chain))
        span.set_attribute("tollgate.dry_run", dry_run)

        # A blocked call is an error from the caller's perspective, and only a
        # span status makes it show up as one in a trace UI's error views.
        # Not applied in dry_run/observe: nothing was actually denied there.
        if decision.action is BLOCK and not dry_run:
            span.set_status(trace.Status(trace.StatusCode.ERROR, decision.reason))

        span_context = span.get_span_context()
        yield (
            trace.format_trace_id(span_context.trace_id),
            trace.format_span_id(span_context.span_id),
        )
