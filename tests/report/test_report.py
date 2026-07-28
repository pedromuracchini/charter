from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK
from tollgate.ledger.event import LedgerEvent
from tollgate.report.graph import delegation_graph, policy_graph
from tollgate.report.narrative import narrative
from tollgate.report.policy_report import build_report


def _event(**overrides):
    base = dict(
        event_id="evt_1",
        ts="2026-06-03T14:32:01Z",
        tool="delete_record",
        args={},
        policy="p1",
        decision="BLOCK",
        reason="r",
        caller_agent_id="executor_agent",
        delegation_chain=["orchestrator", "executor_agent"],
        trust_level=1,
    )
    base.update(overrides)
    return LedgerEvent(**base)


def test_policy_graph_mermaid_contains_edge():
    out = policy_graph([_event()], format="mermaid")
    assert "block" in out
    assert "delete_record" in out


def test_policy_graph_dot_contains_edge():
    out = policy_graph([_event()], format="dot")
    assert "->" in out


def test_delegation_graph_contains_agent_edge():
    out = delegation_graph([_event()], format="mermaid")
    assert "trust=1" in out


def test_narrative_non_technical():
    text = narrative([_event()])
    assert "blocked" in text


def test_narrative_empty():
    assert "No tool calls" in narrative([])


def test_build_report_coverage_dynamic_only():
    policy = PolicySet("p1")
    policy.require(lambda ctx: True, on_fail=BLOCK, reason="r")
    report = build_report(
        [policy], [_event()], all_tool_names=["delete_record", "read_record"], include_static=False
    )
    assert report.coverage_ratio == 0.5
    assert "delete_record" in report.covered_tools
    assert "read_record" in report.uncovered_tools


def test_build_report_coverage_includes_static_by_default():
    # p1 has no tool-specific active_when, so it statically applies to every tool.
    policy = PolicySet("p1")
    policy.require(lambda ctx: True, on_fail=BLOCK, reason="r")
    report = build_report([policy], [_event()], all_tool_names=["delete_record", "read_record"])
    assert report.coverage_ratio == 1.0
    assert "read_record" in report.covered_tools
    assert not report.uncovered_tools


def test_build_report_static_coverage_without_any_events():
    from tollgate.multiagent.scoped_policy import AgentScopedPolicy

    policy = AgentScopedPolicy(
        name="only_admin",
        on_fail=BLOCK,
        reason="admin only",
        allowed_roles=["admin"],
        applies_to=lambda ctx: ctx.tool_name == "delete_record",
    )
    report = build_report([policy], [], all_tool_names=["delete_record", "read_record"])
    assert "delete_record" in report.covered_tools
    assert "read_record" in report.uncovered_tools
    assert report.policies[0].block_count == 0  # never actually fired yet


def test_build_report_window_hours_excludes_older_events():
    from datetime import UTC, datetime, timedelta

    old_event = _event(ts=(datetime.now(UTC) - timedelta(hours=48)).isoformat())
    policy = PolicySet("p1")
    policy.require(lambda ctx: True, on_fail=BLOCK, reason="r")

    report = build_report([policy], [old_event], window_hours=24)
    assert report.policies[0].block_count == 0

    report_wide = build_report([policy], [old_event], window_hours=72)
    assert report_wide.policies[0].block_count == 1
    assert report_wide.window_hours == 72
