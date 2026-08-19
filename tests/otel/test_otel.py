import pytest

from chokepoint._engine import evaluate_call
from chokepoint._scope import ExecutionScope, current_scope
from chokepoint.core.context import GuardContext
from chokepoint.core.policy_set import PolicySet
from chokepoint.decisions import ALLOW, BLOCK, ESCALATE, GuardBlocked, GuardDecision
from chokepoint.ledger.ledger import ActionLedger
from chokepoint.otel.config import configure_otel, otel_available, reset_otel
from chokepoint.otel.spans import evaluate_span, should_sample

#: Everything below that actually inspects a span or a metric needs the SDK
#: from the `otel` extra. Skipping says so; the `if not otel_available():
#: return` these tests used to open with reported a pass having asserted
#: nothing, so a bare-environment run looked fully green.
requires_otel = pytest.mark.skipif(not otel_available(), reason="requires the `otel` extra")


def _ctx():
    return GuardContext.build(tool_name="t", args={}, scope=ExecutionScope())


def test_unconfigured_otel_yields_no_span():
    reset_otel()
    decision = GuardDecision(action=ALLOW, reason="r")
    with evaluate_span(_ctx(), decision, "pre", dry_run=False, latency_ms=0.0) as span_ids:
        assert span_ids is None


@requires_otel
def test_configured_otel_with_in_memory_exporter_emits_span():
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
    assert spans[0].name == "chokepoint.evaluate"
    reset_otel()


@requires_otel
def test_per_call_tracer_provider_override_works_without_configure_otel():
    """A `tracer_provider` passed directly to `evaluate_span` (as
    `ChokepointInterceptor.otel_tracer` does) must emit spans even if the global
    `configure_otel()` was never called."""
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


@requires_otel
def test_allow_sampling_is_rolled_once_not_squared():
    """_record_allow rolled at allow_sample_rate, then evaluate_span rolled
    again at the same rate — the effective span rate was allow_sample_rate**2,
    and the ledger and spans disagreed about which events survived."""
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


@requires_otel
def test_escalate_spans_sample_at_the_block_rate_not_the_allow_rate():
    """An ESCALATE is a failure. It used to fall through to allow_sample_rate,
    so dialing allows down silently thinned approval-request spans too."""
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


#: OTEL refuses to replace an already-installed global meter provider, and
#: `metrics.py` reads the global one (unlike spans, which take a provider
#: argument). So install exactly one for the whole module and drain it per test.
_METRIC_READER = None


def _in_memory_metrics():
    """The process-wide in-memory metric reader, drained and ready to use."""
    global _METRIC_READER
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    if _METRIC_READER is None:
        from opentelemetry.sdk.metrics import Counter, Histogram
        from opentelemetry.sdk.metrics.export import AggregationTemporality

        # DELTA, so draining actually clears: the default CUMULATIVE keeps
        # every earlier test's data points alive in the next read.
        _METRIC_READER = InMemoryMetricReader(
            preferred_temporality={
                Counter: AggregationTemporality.DELTA,
                Histogram: AggregationTemporality.DELTA,
            }
        )
        metrics.set_meter_provider(MeterProvider(metric_readers=[_METRIC_READER]))
    _METRIC_READER.get_metrics_data()  # drain whatever earlier tests emitted
    return _METRIC_READER


def _collect(reader):
    """Drain the reader once, into {metric_name: [data_points]}.

    Reading is destructive under DELTA temporality, so a test must collect
    everything in a single call rather than querying metric by metric.
    """
    collected: dict[str, list] = {}
    data = reader.get_metrics_data()
    if data is None:
        return collected
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                collected.setdefault(metric.name, []).extend(metric.data.data_points)
    return collected


@requires_otel
def test_span_carries_the_tool_name_and_marks_a_block_as_an_error():
    """Without chokepoint.tool you cannot group spans by tool in a backend, and
    without a span status a BLOCK never surfaces in error views."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    configure_otel(tracer_provider=provider)

    policy = PolicySet("blocks")
    policy.require(lambda ctx: False, on_fail=BLOCK, reason="denied")
    with pytest.raises(GuardBlocked):
        evaluate_call(
            tool_name="delete_bucket",
            args={},
            invoke=lambda: None,
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )

    span = exporter.get_finished_spans()[0]
    assert span.attributes["chokepoint.tool"] == "delete_bucket"
    assert span.attributes["chokepoint.session_id"] == "default"
    assert span.status.status_code is StatusCode.ERROR
    reset_otel()


@requires_otel
def test_dry_run_block_is_not_marked_as_a_span_error():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    configure_otel(tracer_provider=provider)

    policy = PolicySet("blocks")
    policy.require(lambda ctx: False, on_fail=BLOCK, reason="denied")
    evaluate_call(
        tool_name="t",
        args={},
        invoke=lambda: None,
        policies=[policy],
        mode="dry_run",
        scope=current_scope(),
    )

    assert exporter.get_finished_spans()[0].status.status_code is not StatusCode.ERROR
    reset_otel()


@requires_otel
def test_escalation_metrics_are_emitted_with_the_outcome():
    """There was previously no escalation metric at all — the one decision
    that puts a human in the request path was unobservable."""
    from chokepoint.core.escalation import EscalationHandler, register_handler

    class Approve(EscalationHandler):
        def escalate(self, ctx, rule_result):
            return True

    register_handler("metrics-scheme", Approve())
    reader = _in_memory_metrics()
    configure_otel()

    policy = PolicySet("needs_approval")
    policy.require(
        lambda ctx: False,
        on_fail=ESCALATE,
        reason="approve me",
        escalate_to="metrics-scheme://ops",
    )
    evaluate_call(
        tool_name="transfer",
        args={},
        invoke=lambda: None,
        policies=[policy],
        mode="enforce",
        scope=current_scope(),
    )

    collected = _collect(reader)
    points = collected["chokepoint.escalations_total"]
    assert len(points) == 1
    assert points[0].attributes["outcome"] == "approved"
    assert points[0].attributes["tool"] == "transfer"
    assert points[0].attributes["escalate_to"] == "metrics-scheme://ops"
    assert collected["chokepoint.escalation_latency_ms"]
    reset_otel()


@requires_otel
def test_dry_run_escalation_is_recorded_as_not_resolved():
    reader = _in_memory_metrics()
    configure_otel()

    policy = PolicySet("needs_approval")
    policy.require(lambda ctx: False, on_fail=ESCALATE, reason="approve me", escalate_to="unrouted://x")
    evaluate_call(
        tool_name="transfer",
        args={},
        invoke=lambda: None,
        policies=[policy],
        mode="dry_run",
        scope=current_scope(),
    )

    points = _collect(reader)["chokepoint.escalations_total"]
    assert [p.attributes["outcome"] for p in points] == ["not_resolved"]
    reset_otel()


@requires_otel
def test_delegation_depth_metric_is_actually_emitted():
    """record_delegation_depth() existed and was documented but no call site
    ever invoked it."""
    reader = _in_memory_metrics()
    configure_otel()

    scope = ExecutionScope(caller_agent_id="executor", delegation_chain=("orchestrator", "executor"))
    evaluate_call(tool_name="t", args={}, invoke=lambda: None, policies=[], mode="enforce", scope=scope)

    points = _collect(reader)["chokepoint.delegation_depth"]
    assert len(points) == 1
    assert points[0].attributes["agent"] == "executor"
    assert points[0].sum == 1  # one hop, not two chain entries
    reset_otel()


# --- sampling boundaries ---------------------------------------------------
# `should_sample` only reads the configured rates, so these hold with or
# without the `otel` extra installed.


def test_an_allow_rate_of_zero_never_samples():
    """The comparison used to be `<=`, and `random()` can return exactly 0.0 —
    a rate the caller explicitly turned off still let the occasional draw
    through."""
    configure_otel(allow_sample_rate=0.0, block_sample_rate=1.0)
    decision = GuardDecision(action=ALLOW, reason="r")
    assert not any(should_sample(decision) for _ in range(2000))
    reset_otel()


def test_an_allow_rate_of_one_always_samples():
    """`random()` never returns 1.0, so a strict `<` still samples everything."""
    configure_otel(allow_sample_rate=1.0, block_sample_rate=1.0)
    decision = GuardDecision(action=ALLOW, reason="r")
    assert all(should_sample(decision) for _ in range(2000))
    reset_otel()


def test_a_block_rate_of_zero_never_samples():
    configure_otel(allow_sample_rate=1.0, block_sample_rate=0.0)
    decision = GuardDecision(action=BLOCK, reason="denied")
    assert not any(should_sample(decision) for _ in range(2000))
    reset_otel()
