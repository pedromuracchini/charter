import pytest

from tollgate._scope import ExecutionScope
from tollgate.core.context import GuardContext
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import ALLOW, BLOCK, ESCALATE


def _ctx():
    return GuardContext.build(tool_name="t", args={}, scope=ExecutionScope())


def _policy(name, passes, on_fail=BLOCK):
    p = PolicySet(name)
    p.require(lambda ctx: passes, on_fail=on_fail, reason=f"{name} failed")
    return p


def _failing(policy, ctx, hook="pre"):
    return [r for r in policy.evaluate(ctx, hook) if not r.passed]


@pytest.mark.parametrize(
    "a_passes,b_passes,expect_failure",
    [
        (True, True, False),
        (True, False, True),
        (False, True, True),
        (False, False, True),
    ],
)
def test_and_policy_truth_table(a_passes, b_passes, expect_failure):
    combined = _policy("a", a_passes) & _policy("b", b_passes)
    assert bool(_failing(combined, _ctx())) == expect_failure


@pytest.mark.parametrize(
    "a_passes,b_passes,expect_failure",
    [
        (True, True, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
)
def test_or_policy_truth_table(a_passes, b_passes, expect_failure):
    combined = _policy("a", a_passes) | _policy("b", b_passes)
    assert bool(_failing(combined, _ctx())) == expect_failure


@pytest.mark.parametrize("a_passes,expect_failure", [(True, True), (False, False)])
def test_not_policy_truth_table(a_passes, expect_failure):
    negated = ~_policy("a", a_passes)
    assert bool(_failing(negated, _ctx())) == expect_failure


def test_policy_hash_is_stable_and_changes_with_rules():
    def predicate(ctx):
        return True

    a = PolicySet("a")
    a.require(predicate, on_fail=BLOCK, reason="r1")

    b = PolicySet("a")
    b.require(predicate, on_fail=BLOCK, reason="r1")

    assert a.policy_hash == b.policy_hash
    assert a.policy_hash.startswith("sha256:")

    c = PolicySet("a")
    c.require(predicate, on_fail=BLOCK, reason="different reason")
    assert c.policy_hash != a.policy_hash


def test_composite_policy_hash_changes_with_children():
    def predicate(ctx):
        return True

    a = PolicySet("a")
    a.require(predicate, on_fail=BLOCK, reason="r1")
    b = PolicySet("b")
    b.require(predicate, on_fail=BLOCK, reason="r2")

    assert (a & b).policy_hash != (a | b).policy_hash
    assert (~a).policy_hash != a.policy_hash


def test_predicate_exception_fails_closed_regardless_of_declared_on_fail():
    def broken(ctx):
        raise ZeroDivisionError("boom")

    for declared_on_fail in (BLOCK, ESCALATE, ALLOW):
        p = PolicySet("broken")
        p.require(broken, on_fail=declared_on_fail, reason="should never crash")
        results = p.evaluate(_ctx(), "pre")
        assert len(results) == 1
        assert results[0].passed is False
        assert results[0].on_fail is BLOCK
        assert results[0].severity == "high"
        assert "ZeroDivisionError" in results[0].reason


def test_active_when_exception_treats_policy_as_active():
    def broken_active_when(ctx):
        raise RuntimeError("boom")

    p = PolicySet("broken_active_when", active_when=broken_active_when)
    p.require(lambda ctx: True, on_fail=BLOCK, reason="r")
    assert p.is_active(_ctx()) is True
    # still evaluates rules normally since is_active fails safe to True
    results = p.evaluate(_ctx(), "pre")
    assert len(results) == 1
    assert results[0].passed is True


def test_not_policy_is_not_applicable_when_child_has_no_rules_for_this_hook():
    """Regression test: a PolicySet's evaluate() always includes an entry per
    matching rule, whether it passed or failed — an empty result means "not
    applicable" (inactive, or no rules registered for this hook), never
    "applicable and everything passed". NotPolicy must not confuse the two:
    a child with only "pre" rules must leave `~child` not-applicable on the
    "post" hook too, not synthesize a block there."""
    child = PolicySet("pre_only")
    child.require(lambda ctx: True, on_fail=BLOCK, reason="r", hook="pre")
    negated = ~child

    assert child.evaluate(_ctx(), "post") == []
    assert negated.evaluate(_ctx(), "post") == []


def test_not_policy_still_blocks_when_child_actually_passes_on_the_hook():
    child = PolicySet("pre_only")
    child.require(lambda ctx: True, on_fail=BLOCK, reason="r", hook="pre")
    negated = ~child

    failing = _failing(negated, _ctx(), hook="pre")
    assert failing
    assert "raised no violation" in failing[0].reason
