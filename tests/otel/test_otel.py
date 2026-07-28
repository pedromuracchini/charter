from tollgate._engine import evaluate_call
from tollgate._scope import ExecutionScope, current_scope
from tollgate.core.context import GuardContext
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import ALLOW, BLOCK, ESCALATE, GuardDecision
from tollgate.ledger.ledger import ActionLedger
from tollgate.otel.config import configure_otel, otel_available, reset_otel
from tollgate.otel.spans import evaluate_span, should_sample


def _ctx():
    return GuardContext.build(tool_name="t", args={}, scope=ExecutionScope())


def test_unconfigured_otel_yields_no_span():
    reset_otel()
    decision = GuardDecision(action=ALLOW, reason="r")
    with evaluate_span(_ctx(), decision, "pre", dry_run=False, latency_ms=0.0) as span_ids:
        assert span_ids is None


def test_configured_otel_with_in_memory_exporter_emits_span():
    if not otel_available():
        return

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    configure_otel(tracer_provider=provider, allow_sample_rate=1.0, block_sample_rate=1.0)

    decision = GuardDecision(action=ALLOW, reason="r", policy_name="p")
    with evaluate_span(_ctx(), decision, "pre", dry_run=False, latency_ms=1.0) as span_ids:
        assert span_ids is not None

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "tollgate.evaluate"
    reset_otel()


def test_per_call_tracer_provider_override_works_without_configure_otel():
    """A `tracer_provider` passed directly to `evaluate_span` (as
    `TollgateInterceptor.otel_tracer` does) must emit spans even if the global
    `configure_otel()` was never called."""
    if not otel_available():
        return

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    reset_otel()  # simulate configure_otel() never having been called
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    decision = GuardDecision(action=ALLOW, reason="r", policy_name="p")
    with evaluate_span(
        _ctx(), decision, "pre", dry_run=False, latency_ms=1.0, tracer_provider=provider
    ) as span_ids:
        assert span_ids is not None

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    reset_otel()


def test_allow_sampling_is_rolled_once_not_squared():
    """_record_allow rolled at allow_sample_rate, then evaluate_span rolled
    again at the same rate — the effective span rate was allow_sample_rate**2,
    and the ledger and spans disagreed about which events survived."""
    if not otel_available():
        return

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    configure_otel(tracer_provider=provider, allow_sample_rate=0.5, block_sample_rate=1.0)

    policy = PolicySet("passes")
    policy.require(lambda ctx: True, on_fail=BLOCK, reason="fine")
    for _ in range(400):
        evaluate_call(
            tool_name="t",
            args={},
            invoke=lambda: None,
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )

    recorded = len(ActionLedger.current().events())
    spanned = len(exporter.get_finished_spans())

    # Every recorded ALLOW must have a span: one roll, one outcome.
    assert spanned == recorded
    # And the rate is ~0.5, not 0.25. Generous bounds — this is a random draw.
    assert 120 < recorded < 280, recorded
    reset_otel()


def test_escalate_spans_sample_at_the_block_rate_not_the_allow_rate():
    """An ESCALATE is a failure. It used to fall through to allow_sample_rate,
    so dialing allows down silently thinned approval-request spans too."""
    if not otel_available():
        return

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    configure_otel(tracer_provider=provider, allow_sample_rate=0.0, block_sample_rate=1.0)

    decision = GuardDecision(action=ESCALATE, reason="needs approval", policy_name="p")
    with evaluate_span(
        _ctx(), decision, "pre", dry_run=False, latency_ms=1.0, sampled=should_sample(decision)
    ) as span_ids:
        assert span_ids is not None

    assert len(exporter.get_finished_spans()) == 1
    reset_otel()
