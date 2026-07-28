from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK
from tollgate.testing.repl import evaluate_synthetic


def test_evaluate_synthetic_no_policies_allows():
    trace = evaluate_synthetic("t", {}, [])
    assert trace.decision is None
    assert trace.results == ()


def test_evaluate_synthetic_reports_failing_rule():
    policy = PolicySet("p")
    policy.require(lambda ctx: ctx.args.get("x", 0) > 0, on_fail=BLOCK, reason="x must be positive")
    trace = evaluate_synthetic("t", {"x": -1}, [policy], caller_role="executor")
    assert trace.decision is not None
    assert trace.decision.on_fail is BLOCK
    assert trace.context.caller_role == "executor"
