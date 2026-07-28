import pytest

from tollgate.ledger.ledger import ActionLedger
from tollgate.otel.config import reset_otel


@pytest.fixture(autouse=True)
def _reset_global_state():
    ActionLedger.reset()
    reset_otel()
    yield
    ActionLedger.reset()
    reset_otel()
