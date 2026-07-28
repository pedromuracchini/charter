from tollgate._scope import ExecutionScope
from tollgate.core.context import GuardContext
from tollgate.decisions import ALLOW, GuardDecision
from tollgate.otel.config import configure_otel, otel_available, reset_otel
from tollgate.otel.spans import evaluate_span


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
