import threading

import pytest

from tollgate._engine import evaluate_call
from tollgate._scope import current_scope
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK, GuardBlocked
from tollgate.ledger.event import LedgerEvent
from tollgate.ledger.ledger import ActionLedger, replay


def _event(event_id="evt_1", decision="BLOCK", policy="p", tool="t"):
    return LedgerEvent(
        event_id=event_id,
        ts="2026-06-03T14:32:01Z",
        tool=tool,
        args={"id": 1},
        policy=policy,
        decision=decision,
        reason="r",
    )


def test_record_and_get():
    ledger = ActionLedger.current()
    event = ledger.record(_event())
    assert ledger.get("evt_1") == event
    assert ledger.get("missing") is None


def test_export_json_and_csv_round_trip_shape():
    ledger = ActionLedger.current()
    ledger.record(_event())
    as_json = ledger.export_compliance_report(format="json")
    assert '"event_id": "evt_1"' in as_json
    as_csv = ledger.export_compliance_report(format="csv")
    assert "event_id" in as_csv.splitlines()[0]
    assert "evt_1" in as_csv


def test_export_compliance_report_rejects_unknown_format():
    with pytest.raises(ValueError):
        ActionLedger.current().export_compliance_report(format="pdf")  # type: ignore[arg-type]


def test_replay_without_policies_reconstructs_context():
    ActionLedger.current().record(_event())
    result = replay("evt_1")
    assert result.context.tool_name == "t"
    assert result.original_decision == "BLOCK"
    assert result.new_results is None
    assert result.changed is False


def test_replay_with_policies_detects_change():
    ActionLedger.current().record(_event())
    policy = PolicySet("p")
    policy.require(lambda ctx: True, on_fail=BLOCK, reason="now passes")
    result = replay("evt_1", policies=[policy])
    assert result.changed is True


def test_replay_missing_event_raises():
    with pytest.raises(KeyError):
        replay("nonexistent")


def test_max_events_evicts_oldest_and_tracks_dropped_count():
    ledger = ActionLedger(max_events=3)
    for i in range(5):
        ledger.record(_event(event_id=f"evt_{i}"))

    events = ledger.events()
    assert [e.event_id for e in events] == ["evt_2", "evt_3", "evt_4"]
    assert ledger.dropped_count == 2
    assert ledger.get("evt_0") is None
    assert ledger.get("evt_4") is not None


def test_max_events_none_is_unbounded():
    ledger = ActionLedger(max_events=None)
    for i in range(50):
        ledger.record(_event(event_id=f"evt_{i}"))
    assert len(ledger.events()) == 50
    assert ledger.dropped_count == 0


def test_sink_path_captures_full_history_despite_in_memory_cap(tmp_path):
    sink = tmp_path / "ledger.jsonl"
    ledger = ActionLedger(sink_path=sink, max_events=2)
    for i in range(5):
        ledger.record(_event(event_id=f"evt_{i}"))

    assert len(ledger.events()) == 2
    assert ledger.dropped_count == 3

    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5


def test_configure_makes_sink_path_reachable_by_the_engine(tmp_path):
    """`current()` builds its lazy default with no arguments, so a hand-built
    ActionLedger(sink_path=...) is never the one the engine writes to."""
    sink = tmp_path / "ledger.jsonl"
    ActionLedger.configure(sink_path=sink)

    assert ActionLedger.current().sink_path == sink

    policy = PolicySet("blocks")
    policy.require(lambda ctx: False, on_fail=BLOCK, reason="nope")
    with pytest.raises(GuardBlocked):
        evaluate_call(
            tool_name="t",
            args={"a": 1},
            invoke=lambda: None,
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )

    lines = [line for line in sink.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert LedgerEvent.model_validate_json(lines[0]).decision == "BLOCK"


def test_configure_survives_concurrent_current_calls():
    """`current()` used to create the singleton without a lock — two threads
    racing on the first call each built one, and one set of events was lost."""
    ActionLedger._singleton = None
    seen: list[ActionLedger] = []
    barrier = threading.Barrier(8)

    def grab():
        barrier.wait()
        seen.append(ActionLedger.current())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({id(ledger) for ledger in seen}) == 1


def test_reset_drops_configured_sink(tmp_path):
    ActionLedger.configure(sink_path=tmp_path / "x.jsonl")
    ActionLedger.reset()
    assert ActionLedger.current().sink_path is None
