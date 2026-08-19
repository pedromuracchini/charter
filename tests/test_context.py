from chokepoint._scope import ExecutionScope
from chokepoint.core.context import GuardContext


def test_state_checksum_matches_fails_safe_without_provider():
    scope = ExecutionScope(state_checksum="abc")
    ctx = GuardContext.build(tool_name="t", args={}, scope=scope)
    assert ctx.state_checksum_matches() is False


def test_state_checksum_matches_with_provider():
    scope = ExecutionScope(state_checksum="abc", checksum_provider=lambda: "abc")
    ctx = GuardContext.build(tool_name="t", args={}, scope=scope)
    assert ctx.state_checksum_matches() is True

    scope2 = ExecutionScope(state_checksum="abc", checksum_provider=lambda: "different")
    ctx2 = GuardContext.build(tool_name="t", args={}, scope=scope2)
    assert ctx2.state_checksum_matches() is False


def test_patient_consent_fails_safe_without_provider():
    ctx = GuardContext.build(tool_name="t", args={}, scope=ExecutionScope())
    assert ctx.patient_consent_on_file("patient_1") is False
    assert ctx.patient_consent_on_file(None) is False


def test_patient_consent_with_provider():
    scope = ExecutionScope(consent_provider=lambda pid: pid == "patient_1")
    ctx = GuardContext.build(tool_name="t", args={}, scope=scope)
    assert ctx.patient_consent_on_file("patient_1") is True
    assert ctx.patient_consent_on_file("patient_2") is False


def test_is_delegated_from_and_trust_at_least():
    scope = ExecutionScope(delegation_chain=("orchestrator", "executor_agent"), trust_level=1)
    ctx = GuardContext.build(tool_name="t", args={}, scope=scope)
    assert ctx.is_delegated_from("orchestrator") is True
    assert ctx.is_delegated_from("nobody") is False
    assert ctx.trust_at_least(1) is True
    assert ctx.trust_at_least(2) is False
