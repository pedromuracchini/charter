from tollgate._scope import ExecutionScope
from tollgate.core.context import GuardContext
from tollgate.decisions import ALLOW, BLOCK, ESCALATE
from tollgate.multiagent.delegation import delegation_depth, extend_chain, max_delegation_depth_policy
from tollgate.multiagent.registry import TollgateRegistry
from tollgate.multiagent.scoped_policy import AgentScopedPolicy


def _ctx(**scope_kwargs):
    scope = ExecutionScope(**scope_kwargs)
    return GuardContext.build(tool_name="delete_patient", args={}, scope=scope)


def test_agent_scoped_policy_blocks_disallowed_role():
    policy = AgentScopedPolicy(
        name="only_physician",
        on_fail=BLOCK,
        reason="restricted",
        allowed_roles=["licensed_physician"],
        applies_to=lambda ctx: ctx.tool_name == "delete_patient",
    )
    failing = [r for r in policy.evaluate(_ctx(caller_role="support_staff"), "pre") if not r.passed]
    assert failing

    failing2 = [r for r in policy.evaluate(_ctx(caller_role="licensed_physician"), "pre") if not r.passed]
    assert not failing2


def test_max_delegation_depth_policy():
    policy = max_delegation_depth_policy(2)
    ctx = _ctx(delegation_chain=("orchestrator", "research_agent", "writer_agent"))
    assert delegation_depth(ctx) == 3
    failing = [r for r in policy.evaluate(ctx, "pre") if not r.passed]
    assert failing

    ctx2 = _ctx(delegation_chain=("orchestrator", "executor_agent"))
    failing2 = [r for r in policy.evaluate(ctx2, "pre") if not r.passed]
    assert not failing2


def test_extend_chain():
    assert extend_chain(("a",), "b") == ("a", "b")


def test_registry_register_and_get():
    registry = TollgateRegistry()
    registry.register("agent1", role="executor", trust_level=1)
    identity = registry.get("agent1")
    assert identity is not None
    assert identity.role == "executor"
    assert registry.get("missing") is None
    assert "agent1" in registry


def test_agent_scoped_policy_predicate_exception_fails_closed():
    def broken_pre(ctx):
        raise ValueError("boom")

    for declared_on_fail in (BLOCK, ESCALATE, ALLOW):
        policy = AgentScopedPolicy(
            name="broken",
            on_fail=declared_on_fail,
            reason="should never crash",
            pre=broken_pre,
        )
        results = policy.evaluate(_ctx(), "pre")
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].on_fail is BLOCK
        assert results[0].severity == "high"
        assert "ValueError" in results[0].reason


def test_agent_scoped_policy_applies_to_exception_treats_as_active():
    def broken_applies_to(ctx):
        raise RuntimeError("boom")

    policy = AgentScopedPolicy(
        name="broken_applies_to",
        on_fail=BLOCK,
        reason="r",
        pre=lambda ctx: True,
        applies_to=broken_applies_to,
    )
    assert policy.is_active(_ctx()) is True
    results = policy.evaluate(_ctx(), "pre")
    assert len(results) == 1
    assert results[0].passed is True
