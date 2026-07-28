import pytest

from tollgate.ledger.event import LedgerEvent
from tollgate.redaction import DEFAULT_PLACEHOLDER
from tollgate.testing.harness import fixtures_from_events


def _event(event_id="evt_1", policy="p", tool="t", decision="BLOCK", args=None):
    return LedgerEvent(
        event_id=event_id,
        ts="2026-06-03T14:32:01Z",
        tool=tool,
        args={} if args is None else args,
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
    with pytest.raises(ValueError):
        fixtures_from_events([], framework="junit")  # type: ignore[arg-type]


def test_fixtures_from_events_deduplicates_identical_combinations():
    out = fixtures_from_events([_event(), _event(event_id="evt_2")])
    assert out.count("def test_") == 1
    # The last event seen for a combination is the one replayed.
    assert "replay('evt_2'" in out


def test_redacted_events_are_emitted_as_skipped_tests():
    """Dropping them would overstate coverage; keeping them runnable would
    generate tests that feed placeholders to the predicates and always fail."""
    out = fixtures_from_events([_event(args={"token": DEFAULT_PLACEHOLDER})])
    assert '@pytest.mark.skip(reason="arguments were redacted; replay is not comparable")' in out
    assert "import pytest" in out
    assert "replay('evt_1'" in out


def test_a_redacted_event_only_skips_its_own_test():
    out = fixtures_from_events(
        [
            _event(event_id="evt_clean", tool="clean"),
            _event(event_id="evt_redacted", tool="secretive", args={"token": DEFAULT_PLACEHOLDER}),
        ]
    )
    assert out.count("@pytest.mark.skip") == 1
    skip_line = next(i for i, line in enumerate(out.splitlines()) if "@pytest.mark.skip" in line)
    assert "evt_redacted" in out.splitlines()[skip_line + 2]


def test_fixtures_without_redaction_do_not_import_pytest():
    out = fixtures_from_events([_event()])
    assert "import pytest" not in out
    assert "@pytest.mark.skip" not in out


def test_generated_fixtures_are_valid_python_with_and_without_redaction():
    for events in ([_event()], [_event(args={"token": DEFAULT_PLACEHOLDER})]):
        compile(fixtures_from_events(events), "<generated>", "exec")


def test_fixtures_from_no_events_is_still_a_valid_module():
    compile(fixtures_from_events([]), "<generated>", "exec")
