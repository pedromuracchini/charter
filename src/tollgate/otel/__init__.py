"""OpenTelemetry spans and metrics. No-ops without the `otel` extra."""

from tollgate.otel.config import OtelSettings, configure_otel, current_settings, otel_available, reset_otel
from tollgate.otel.metrics import record_decision, record_delegation_depth, record_escalation
from tollgate.otel.spans import evaluate_span, should_sample

__all__ = [
    "OtelSettings",
    "configure_otel",
    "current_settings",
    "evaluate_span",
    "otel_available",
    "record_decision",
    "record_delegation_depth",
    "record_escalation",
    "reset_otel",
    "should_sample",
]
