from charter.core.policy_set import PolicySet
from charter.decisions import BLOCK
from charter.linter.linter import lint
from charter.multiagent.registry import CharterRegistry
from charter.multiagent.scoped_policy import AgentScopedPolicy


def test_dead_policy_detected():
    empty_policy = PolicySet("empty")
    findings = lint([empty_policy])
    assert any("never fires" in f.message for f in findings)


def test_duplicate_names_detected():
    a = PolicySet("dup")
    a.require(lambda ctx: True, on_fail=BLOCK, reason="r")
    b = PolicySet("dup")
    b.require(lambda ctx: True, on_fail=BLOCK, reason="r")
    findings = lint([a, b])
    assert any("registered 2 times" in f.message for f in findings)


def test_scoped_policy_without_registry_is_an_error():
    scoped = AgentScopedPolicy(name="x", on_fail=BLOCK, reason="r", allowed_roles=["admin"])
    findings = lint([scoped], registry=None)
    assert any(f.severity == "error" for f in findings)


def test_scoped_policy_with_registry_is_fine():
    scoped = AgentScopedPolicy(name="x", on_fail=BLOCK, reason="r", allowed_roles=["admin"])
    registry = CharterRegistry()
    registry.register("agent1", role="admin")
    findings = lint([scoped], registry=registry)
    assert not any(f.severity == "error" for f in findings)


def test_uncovered_tools_detected():
    policy = AgentScopedPolicy(
        name="x",
        on_fail=BLOCK,
        reason="r",
        applies_to=lambda ctx: ctx.tool_name == "delete_x",
        pre=lambda ctx: True,
    )
    findings = lint([policy], tool_names=["delete_x", "read_y"])
    assert any("read_y" in f.message for f in findings)
    assert not any("delete_x" in f.message for f in findings)


def test_flags_high_action_without_escalate_to():
    from charter.core.reversible import ReversibleAction

    unrouted = ReversibleAction(do_fn=lambda a: a, undo_fn=None, name="wipe_db", irreversibility_level="high")
    routed = ReversibleAction(
        do_fn=lambda a: a,
        undo_fn=None,
        name="delete_bucket",
        irreversibility_level="high",
        escalate_to="slack://ops",
    )
    permanent = ReversibleAction(
        do_fn=lambda a: a, undo_fn=None, name="nuke", irreversibility_level="permanent"
    )

    findings = lint([], actions=[unrouted, routed, permanent])

    assert [f.policy_name for f in findings] == ["wipe_db"]
    assert findings[0].severity == "warning"
    assert "escalate_to" in findings[0].message
