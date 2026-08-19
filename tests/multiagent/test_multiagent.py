from chokepoint._scope import ExecutionScope, current_scope
from chokepoint.core.context import GuardContext
from chokepoint.core.interceptor import ChokepointInterceptor
from chokepoint.decisions import ALLOW, BLOCK, ESCALATE
from chokepoint.ledger.ledger import ActionLedger
from chokepoint.multiagent.delegation import delegation_depth, extend_chain, max_delegation_depth_policy
from chokepoint.multiagent.registry import ChokepointRegistry
from chokepoint.multiagent.scoped_policy import AgentScopedPolicy


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


def test_delegation_depth_counts_hops_not_chain_entries():
    """The chain is self-inclusive, so a call an agent makes directly is depth
    0 — otherwise every threshold would silently shift by one hop."""
    assert delegation_depth(_ctx(delegation_chain=())) == 0
    assert delegation_depth(_ctx(delegation_chain=("solo_agent",))) == 0
    assert delegation_depth(_ctx(delegation_chain=("orchestrator", "executor"))) == 1
    assert delegation_depth(_ctx(delegation_chain=("a", "b", "c", "d"))) == 3


def test_max_delegation_depth_policy():
    policy = max_delegation_depth_policy(2)
    ctx = _ctx(delegation_chain=("orchestrator", "research_agent", "writer_agent", "sub_agent"))
    assert delegation_depth(ctx) == 3
    failing = [r for r in policy.evaluate(ctx, "pre") if not r.passed]
    assert failing

    ctx2 = _ctx(delegation_chain=("orchestrator", "executor_agent"))
    failing2 = [r for r in policy.evaluate(ctx2, "pre") if not r.passed]
    assert not failing2


def test_extend_chain():
    assert extend_chain(("a",), "b") == ("a", "b")


def test_registry_register_and_get():
    registry = ChokepointRegistry()
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


def test_scope_chain_includes_the_acting_agent():
    """The registry records ancestors only, but every consumer of the chain —
    the delegation graph's zip(chain, chain[1:]), _is_cross_agent, the ledger's
    documented shape — reads it as the full path."""
    registry = ChokepointRegistry()
    registry.register("orchestrator", role="orchestrator")
    registry.register("executor", role="worker", delegation_chain=("orchestrator",))

    seen = {}

    def probe():
        seen.update(chain=list(current_scope().delegation_chain))

    ChokepointInterceptor(registry=registry, agent_id="executor").call("probe", probe)
    assert seen["chain"] == ["orchestrator", "executor"]


def test_root_agent_chain_is_just_itself():
    registry = ChokepointRegistry()
    registry.register("orchestrator", role="orchestrator")

    seen = {}
    ChokepointInterceptor(registry=registry, agent_id="orchestrator").call(
        "probe", lambda: seen.update(chain=list(current_scope().delegation_chain))
    )
    assert seen["chain"] == ["orchestrator"]


def test_an_already_self_inclusive_chain_is_not_doubled():
    """Code written against the old convention passed self-inclusive chains
    explicitly to work around the graph bug — it must keep working."""
    registry = ChokepointRegistry()
    registry.register("executor", delegation_chain=("orchestrator", "executor"))

    seen = {}
    ChokepointInterceptor(registry=registry, agent_id="executor").call(
        "probe", lambda: seen.update(chain=list(current_scope().delegation_chain))
    )
    assert seen["chain"] == ["orchestrator", "executor"]


def test_direct_delegation_now_produces_a_graph_edge():
    """zip(chain, chain[1:]) on a one-element ancestors-only tuple yielded
    nothing, so a parent->child relationship drew zero agent edges."""
    from chokepoint.core.policy_set import PolicySet
    from chokepoint.report.graph import delegation_graph

    registry = ChokepointRegistry()
    registry.register("executor", role="worker", delegation_chain=("orchestrator",))
    # Some policy has to apply, or nothing is recorded and the graph is empty.
    policy = PolicySet("always_ok")
    policy.require(lambda ctx: True, on_fail=BLOCK, reason="fine")
    ChokepointInterceptor(registry=registry, agent_id="executor", policies=[policy]).call(
        "search", lambda: None
    )

    graph = delegation_graph(ActionLedger.current().events(), format="mermaid")
    assert "orchestrator" in graph and "executor" in graph
    assert "-->" in graph
