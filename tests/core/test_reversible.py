import pytest

from tollgate._engine import _maybe_await, evaluate_call, evaluate_call_async
from tollgate._scope import current_scope
from tollgate.core.policy_set import PolicySet
from tollgate.core.reversible import ReversibleAction
from tollgate.decisions import BLOCK, ESCALATE, GuardBlocked
from tollgate.ledger.ledger import ActionLedger


def test_low_executes_normally_no_intrinsic_check():
    action = ReversibleAction(do_fn=lambda a: a, undo_fn=None, name="x", irreversibility_level="low")
    assert action.intrinsic_check() is None
    assert action({"a": 1}) == {"a": 1}


def test_medium_requires_undo_fn_at_construction():
    with pytest.raises(ValueError):
        ReversibleAction(do_fn=lambda a: a, undo_fn=None, name="x", irreversibility_level="medium")

    action = ReversibleAction(
        do_fn=lambda a: a, undo_fn=lambda a, s: None, name="x", irreversibility_level="medium"
    )
    assert action.intrinsic_check() is None


def test_high_always_escalates():
    action = ReversibleAction(do_fn=lambda a: a, undo_fn=None, name="x", irreversibility_level="high")
    check = action.intrinsic_check()
    assert check is not None
    assert check.on_fail is ESCALATE
    assert check.passed is False


def test_permanent_intrinsic_check_always_blocks():
    action = ReversibleAction(do_fn=lambda a: a, undo_fn=None, name="x", irreversibility_level="permanent")
    check = action.intrinsic_check()
    assert check is not None
    assert check.on_fail is BLOCK
    assert check.passed is False


def test_permanent_blocks():
    """The engine must never call do_fn for a permanent ReversibleAction."""
    calls = []
    action = ReversibleAction(
        do_fn=lambda a: calls.append(a), undo_fn=None, name="drop_db", irreversibility_level="permanent"
    )
    with pytest.raises(GuardBlocked):
        evaluate_call(
            tool_name="drop_db",
            args={},
            invoke=lambda: action({}),
            policies=[],
            mode="enforce",
            scope=current_scope(),
            reversible=action,
        )
    assert calls == []


def test_high_auto_escalates_and_fails_safe_to_block():
    calls = []
    action = ReversibleAction(
        do_fn=lambda a: calls.append(a), undo_fn=None, name="delete_bucket", irreversibility_level="high"
    )
    with pytest.raises(GuardBlocked):
        evaluate_call(
            tool_name="delete_bucket",
            args={},
            invoke=lambda: action({}),
            policies=[],
            mode="enforce",
            scope=current_scope(),
            reversible=action,
        )
    assert calls == []  # denied escalation means do_fn is never reached


def test_undo_calls_undo_fn_with_args_and_snapshot():
    received = []
    action = ReversibleAction(
        do_fn=lambda a: {"ok": True},
        undo_fn=lambda a, s: received.append((a, s)),
        name="x",
        irreversibility_level="low",
        pre_snapshot=lambda a: {"snap": a["id"]},
    )
    snapshot = action.snapshot({"id": 1})
    action.undo({"id": 1}, snapshot)
    assert received == [({"id": 1}, {"snap": 1})]


def test_undo_is_noop_without_undo_fn():
    action = ReversibleAction(do_fn=lambda a: a, undo_fn=None, name="x", irreversibility_level="low")
    assert action.undo({"id": 1}, None) is None


def test_undo_failure_does_not_lose_the_ledger_event():
    """A bug in undo_fn must not prevent the post-BLOCK decision from being
    recorded — that's the moment that most needs an audit trail."""
    ActionLedger.reset()

    def broken_undo(args, snapshot):
        raise RuntimeError("undo boom")

    action = ReversibleAction(
        do_fn=lambda a: {"rows_affected": a["rows"]},
        undo_fn=broken_undo,
        name="update_rows",
        irreversibility_level="low",
        pre_snapshot=lambda a: {"before": True},
    )
    post_policy = PolicySet("post_check")
    post_policy.require(
        lambda ctx: ctx.result["rows_affected"] == 1, on_fail=BLOCK, reason="too many rows", hook="post"
    )

    with pytest.raises(GuardBlocked) as exc_info:
        evaluate_call(
            tool_name="update_rows",
            args={"rows": 5},
            invoke=lambda: action({"rows": 5}),
            policies=[post_policy],
            mode="enforce",
            scope=current_scope(),
            reversible=action,
        )

    assert exc_info.value.decision.undo_executed is False
    assert "undo also failed" in exc_info.value.decision.reason

    events = ActionLedger.current().events()
    post_block_events = [e for e in events if e.hook == "post" and e.decision == "BLOCK"]
    assert len(post_block_events) == 1
    assert "FAILED" in post_block_events[0].undo_op


async def test_maybe_await_passes_through_plain_values():
    assert await _maybe_await(42) == 42


async def test_maybe_await_awaits_coroutines():
    async def coro():
        return 42

    assert await _maybe_await(coro()) == 42


async def test_async_reversible_action_do_and_undo_round_trip():
    received = []

    async def async_do(args):
        return {"ok": True, "id": args["id"]}

    async def async_undo(args, snapshot):
        received.append((args, snapshot))

    action = ReversibleAction(
        do_fn=async_do,
        undo_fn=async_undo,
        name="x",
        irreversibility_level="low",
        pre_snapshot=lambda a: {"snap": a["id"]},
    )
    result = await _maybe_await(action({"id": 1}))
    assert result == {"ok": True, "id": 1}

    snapshot = action.snapshot({"id": 1})
    await _maybe_await(action.undo({"id": 1}, snapshot))
    assert received == [({"id": 1}, {"snap": 1})]


async def test_evaluate_call_async_permanent_blocks_without_calling_do_fn():
    calls = []

    async def do_fn(a):
        calls.append(a)

    action = ReversibleAction(do_fn=do_fn, undo_fn=None, name="drop_db", irreversibility_level="permanent")

    async def invoke():
        return await _maybe_await(action({}))

    with pytest.raises(GuardBlocked):
        await evaluate_call_async(
            tool_name="drop_db",
            args={},
            invoke=invoke,
            policies=[],
            mode="enforce",
            scope=current_scope(),
            reversible=action,
        )
    assert calls == []


def test_high_routes_its_intrinsic_escalation_to_escalate_to():
    """Without this, "high" is indistinguishable from "permanent": the intrinsic
    ESCALATE resolves to the fail-safe denier and the action can never run."""
    action = ReversibleAction(
        do_fn=lambda a: a,
        undo_fn=None,
        name="x",
        irreversibility_level="high",
        escalate_to="approvals://bucket-deletions",
        timeout_s=42,
    )
    check = action.intrinsic_check()
    assert check is not None
    assert check.escalate_to == "approvals://bucket-deletions"
    assert check.timeout_s == 42


def test_high_with_an_approving_handler_actually_executes():
    from tollgate.core.escalation import EscalationHandler, register_handler

    class Approve(EscalationHandler):
        def escalate(self, ctx, rule_result):
            return True

    register_handler("high-approve-scheme", Approve())

    calls = []
    action = ReversibleAction(
        do_fn=lambda a: calls.append(a),
        undo_fn=None,
        name="delete_bucket",
        irreversibility_level="high",
        escalate_to="high-approve-scheme://ops",
    )

    evaluate_call(
        tool_name="delete_bucket",
        args={"bucket": "b"},
        invoke=lambda: action({"bucket": "b"}),
        policies=[],
        mode="enforce",
        scope=current_scope(),
        reversible=action,
    )

    assert calls == [{"bucket": "b"}]


def test_high_without_escalate_to_still_fails_safe():
    """The old behavior stays the default — an unrouted escalation denies."""
    action = ReversibleAction(
        do_fn=lambda a: a, undo_fn=None, name="x", irreversibility_level="high"
    )
    check = action.intrinsic_check()
    assert check is not None
    assert check.escalate_to is None
