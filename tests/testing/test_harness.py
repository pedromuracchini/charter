from tollgate.ledger.event import LedgerEvent
from tollgate.testing.harness import fixtures_from_events


def _event(event_id="evt_1", policy="p", tool="t", decision="BLOCK"):
    return LedgerEvent(
        event_id=event_id,
        ts="2026-06-03T14:32:01Z",
        tool=tool,
        args={},
        policy=policy,
        decision=decision,
        reason="r",
    )


def test_fixtures_from_events_renders_one_test_per_combination():
    out = fixtures_from_events([_event(), _event(event_id="evt_2", tool="other")])
    assert "def test_" in out
    assert "replay('evt_1'" in out
    assert "replay('evt_2'" in out


def test_fixtures_from_events_skips_events_without_policy():
    out = fixtures_from_events([_event(policy=None)])
    assert "def test_" not in out


def test_fixtures_from_events_rejects_unknown_framework():
    import pytest

    with pytest.raises(ValueError):
        fixtures_from_events([], framework="junit")  # type: ignore[arg-type]
