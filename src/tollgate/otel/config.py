"""OpenTelemetry configuration entry point.

Every module under `tollgate.otel` degrades to a no-op when the `opentelemetry`
packages aren't installed (the `tollgate[otel]` extra) or when `configure_otel`
hasn't been called — the framework works fully without OTEL, it simply emits no
spans or metrics.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

try:
    import opentelemetry.trace as _trace  # noqa: F401

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False


@dataclass
class OtelSettings:
    enabled: bool = False
    tracer_provider: Any = None
    allow_sample_rate: float = 1.0
    block_sample_rate: float = 1.0


_settings = OtelSettings()
_lock = threading.Lock()


def configure_otel(
    tracer_provider: Any = None,
    allow_sample_rate: float = 1.0,
    block_sample_rate: float = 1.0,
) -> None:
    """Point tollgate at an OTEL tracer provider and configure event sampling.

    Blocks (and escalations) are recorded at `block_sample_rate` (default 1.0 —
    always); allowed calls are sampled at `allow_sample_rate`, so high-volume
    agents can dial down allow-event span/metric volume without losing
    visibility into denials.
    """
    global _settings
    new_settings = OtelSettings(
        enabled=OTEL_AVAILABLE,
        tracer_provider=tracer_provider,
        allow_sample_rate=allow_sample_rate,
        block_sample_rate=block_sample_rate,
    )
    with _lock:
        _settings = new_settings


def reset_otel() -> None:
    """Disable OTEL emission and drop cached metric instruments. For tests."""
    global _settings
    # Imported here, not at module scope: metrics.py imports from this module.
    from tollgate.otel.metrics import reset_instruments

    with _lock:
        _settings = OtelSettings()
    reset_instruments()


def current_settings() -> OtelSettings:
    with _lock:
        return _settings


def otel_available() -> bool:
    return OTEL_AVAILABLE
