import pytest

from chokepoint.adapters import register_default_adapters
from chokepoint.adapters.base import reset_adapters
from chokepoint.core.escalation import reset_handlers
from chokepoint.ledger.ledger import ActionLedger
from chokepoint.otel.config import reset_otel
from chokepoint.redaction import reset_redaction


def _reset_all() -> None:
    ActionLedger.reset()
    reset_otel()
    reset_redaction()
    # The escalation-handler and adapter registries are process-global module
    # state. Without this, a handler registered by one test resolves for every
    # later test that uses the same URI scheme.
    reset_handlers()
    reset_adapters()
    register_default_adapters()


@pytest.fixture(autouse=True)
def _reset_global_state():
    _reset_all()
    yield
    _reset_all()
