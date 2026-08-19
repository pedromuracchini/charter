"""A guarded tool that raises must still leave a trace in the audit trail.

Before this, `invoke()` was called bare: an exception skipped every post-hook
and wrote nothing, so an authorized call that ran and blew up was invisible.
"""

import pytest

from chokepoint._engine import evaluate_call, evaluate_call_async
from chokepoint._scope import current_scope
from chokepoint.core.policy_set import PolicySet
from chokepoint.decisions import BLOCK, GuardBlocked
from chokepoint.ledger.ledger import ActionLedger


def _boom():
    raise RuntimeError("upstream API is down")


def test_tool_exception_is_recorded_and_propagated():
    with pytest.raises(RuntimeError, match="upstream API is down"):
        evaluate_call(
            tool_name="fetch",
            args={"url": "https://x"},
            invoke=_boom,
            policies=[],
            mode="enforce",
            scope=current_scope(),
        )

    events = ActionLedger.current().events()
    assert len(events) == 1
    event = events[0]
    assert event.decision == "ERROR"
    assert event.hook == "invoke"
    assert event.severity == "high"
    assert event.tool == "fetch"
    assert event.args == {"url": "https://x"}
    assert "RuntimeError: upstream API is down" in event.reason


async def test_tool_exception_is_recorded_and_propagated_async():
    async def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await evaluate_call_async(
            tool_name="fetch",
            args={},
            invoke=boom,
            policies=[],
            mode="enforce",
            scope=current_scope(),
        )

    event = ActionLedger.current().events()[-1]
    assert event.decision == "ERROR"
    assert "ValueError: nope" in event.reason


def test_error_event_carries_caller_identity():
    """A failing cross-agent call must still be attributable."""
    from chokepoint._scope import ExecutionScope

    scope = ExecutionScope(
        session_id="s1",
        caller_agent_id="executor",
        caller_role="ops",
        delegation_chain=("orchestrator", "executor"),
        trust_level=3,
    )

    with pytest.raises(RuntimeError):
        evaluate_call(
            tool_name="fetch",
            args={},
            invoke=_boom,
            policies=[],
            mode="enforce",
            scope=scope,
        )

    event = ActionLedger.current().events()[-1]
    assert event.caller_agent_id == "executor"
    assert event.caller_role == "ops"
    assert event.delegation_chain == ["orchestrator", "executor"]
    assert event.trust_level == 3
    assert event.session_id == "s1"


def test_a_blocked_call_never_reaches_the_tool_so_records_no_error():
    policy = PolicySet("blocks")
    policy.require(lambda ctx: False, on_fail=BLOCK, reason="denied")

    with pytest.raises(GuardBlocked):
        evaluate_call(
            tool_name="fetch",
            args={},
            invoke=_boom,
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )

    assert [e.decision for e in ActionLedger.current().events()] == ["BLOCK"]


def test_post_hooks_are_skipped_when_the_tool_raises():
    """The post hook has no result to inspect — it must not run on a value
    that was never produced."""
    post_ran = []
    policy = PolicySet("post")
    policy.require(
        lambda ctx: post_ran.append(ctx) is None,
        on_fail=BLOCK,
        reason="post",
        hook="post",
    )

    with pytest.raises(RuntimeError):
        evaluate_call(
            tool_name="fetch",
            args={},
            invoke=_boom,
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )

    assert post_ran == []
