import pytest

from chokepoint.core.interceptor import ChokepointInterceptor
from chokepoint.decisions import ESCALATE, GuardBlocked
from chokepoint.policies import budget_policy, rate_limit_policy
from chokepoint.state import CallState


def _noop(**kwargs):
    return kwargs


def test_rate_limit_allows_exactly_max_calls_then_blocks():
    interceptor = ChokepointInterceptor(policies=[rate_limit_policy(3)])

    for _ in range(3):
        interceptor.call("t", _noop)

    with pytest.raises(GuardBlocked, match="rate limit reached"):
        interceptor.call("t", _noop)


def test_rate_limit_counts_denied_attempts():
    """Otherwise retrying a denial is free and the limit never binds."""
    interceptor = ChokepointInterceptor(policies=[rate_limit_policy(1)])
    interceptor.call("t", _noop)

    for _ in range(3):
        with pytest.raises(GuardBlocked):
            interceptor.call("t", _noop)

    # Still blocked, not reset by the failed attempts.
    with pytest.raises(GuardBlocked):
        interceptor.call("t", _noop)


def test_rate_limit_is_per_session():
    interceptor = ChokepointInterceptor(policies=[rate_limit_policy(2)])
    interceptor.call("t", _noop, session_id="a")
    interceptor.call("t", _noop, session_id="a")
    with pytest.raises(GuardBlocked):
        interceptor.call("t", _noop, session_id="a")

    interceptor.call("t", _noop, session_id="b")  # a fresh session is unaffected


def test_rate_limit_scoped_to_one_tool_ignores_others():
    interceptor = ChokepointInterceptor(policies=[rate_limit_policy(1, tool_name="send_email")])
    interceptor.call("send_email", _noop)
    for _ in range(5):
        interceptor.call("search", _noop)  # different tool, not counted

    with pytest.raises(GuardBlocked):
        interceptor.call("send_email", _noop)


def test_unscoped_rate_limit_counts_every_tool():
    interceptor = ChokepointInterceptor(policies=[rate_limit_policy(2)])
    interceptor.call("a", _noop)
    interceptor.call("b", _noop)
    with pytest.raises(GuardBlocked):
        interceptor.call("c", _noop)


def test_a_shared_call_state_enforces_one_quota_across_agents():
    shared = CallState()
    policy = rate_limit_policy(2)
    agent_a = ChokepointInterceptor(policies=[policy], agent_id="a", call_state=shared)
    agent_b = ChokepointInterceptor(policies=[policy], agent_id="b", call_state=shared)

    agent_a.call("t", _noop)
    agent_b.call("t", _noop)
    with pytest.raises(GuardBlocked):
        agent_a.call("t", _noop)


def test_rate_limit_can_escalate_instead_of_blocking():
    policy = rate_limit_policy(1, on_fail=ESCALATE, escalate_to="unrouted://x")
    interceptor = ChokepointInterceptor(policies=[policy])
    interceptor.call("t", _noop)
    # No handler registered, so the escalation fail-safe denies.
    with pytest.raises(GuardBlocked, match="escalation denied"):
        interceptor.call("t", _noop)


def test_budget_blocks_the_call_that_would_exceed_the_cap():
    policy = budget_policy(100.0, lambda ctx: ctx.args["amount"], tool_name="transfer")
    interceptor = ChokepointInterceptor(policies=[policy])

    interceptor.call("transfer", _noop, amount=60.0)
    interceptor.call("transfer", _noop, amount=30.0)

    # 90 + 20 > 100 — rejected before running, so the cap is never exceeded.
    with pytest.raises(GuardBlocked, match="budget"):
        interceptor.call("transfer", _noop, amount=20.0)

    # A smaller one that still fits is allowed.
    interceptor.call("transfer", _noop, amount=10.0)


def test_a_blocked_call_is_not_charged_to_the_budget():
    """Spend is recorded in the post hook, which a pre-BLOCK never reaches."""
    policy = budget_policy(100.0, lambda ctx: ctx.args["amount"], tool_name="transfer")
    interceptor = ChokepointInterceptor(policies=[policy])

    interceptor.call("transfer", _noop, amount=95.0)
    with pytest.raises(GuardBlocked):
        interceptor.call("transfer", _noop, amount=50.0)

    # The rejected 50 was not charged, so 5 still fits under the 100 cap.
    interceptor.call("transfer", _noop, amount=5.0)


def test_budget_is_per_session():
    policy = budget_policy(50.0, lambda ctx: ctx.args["amount"], tool_name="transfer")
    interceptor = ChokepointInterceptor(policies=[policy])
    interceptor.call("transfer", _noop, session_id="a", amount=50.0)
    interceptor.call("transfer", _noop, session_id="b", amount=50.0)
    with pytest.raises(GuardBlocked):
        interceptor.call("transfer", _noop, session_id="a", amount=1.0)


def test_a_broken_amount_extractor_fails_closed():
    policy = budget_policy(100.0, lambda ctx: ctx.args["missing"], tool_name="transfer")
    interceptor = ChokepointInterceptor(policies=[policy])
    with pytest.raises(GuardBlocked, match="predicate raised"):
        interceptor.call("transfer", _noop, amount=1.0)


def test_history_policies_allow_when_no_call_state_is_attached():
    """A bare @guard or a replay has no history; reporting zero must allow,
    not block on absent data."""
    from chokepoint._engine import evaluate_call
    from chokepoint._scope import ExecutionScope

    policy = rate_limit_policy(1)
    for _ in range(5):
        evaluate_call(
            tool_name="t",
            args={},
            invoke=lambda: None,
            policies=[policy],
            mode="enforce",
            scope=ExecutionScope(),  # no call_state
        )
