import pytest

from charter._engine import _maybe_await, evaluate_call, evaluate_call_async
from charter._scope import current_scope
from charter.core.escalation import EscalationHandler, register_handler
from charter.core.interceptor import CharterInterceptor
from charter.core.policy_set import PolicySet
from charter.core.reversible import ReversibleAction
from charter.decisions import BLOCK, ESCALATE, GuardBlocked
from charter.ledger.ledger import ActionLedger


def _post_block_policy(name="post_blocks"):
    policy = PolicySet(name)
    policy.require(lambda ctx: False, on_fail=BLOCK, reason="result rejected", hook="post")
    return policy


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
    from charter.core.escalation import EscalationHandler, register_handler

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
    action = ReversibleAction(do_fn=lambda a: a, undo_fn=None, name="x", irreversibility_level="high")
    check = action.intrinsic_check()
    assert check is not None
    assert check.escalate_to is None


def test_post_block_without_undo_fn_does_not_claim_a_successful_undo():
    """undo() silently no-ops with no undo_fn, but the engine used to record
    "<name>.undo" and undo_executed=True anyway — a false audit record at
    exactly the moment nothing was reverted."""
    action = ReversibleAction(
        do_fn=lambda a: {"deleted": True},
        undo_fn=None,
        name="delete_thing",
        irreversibility_level="low",
    )
    policy = PolicySet("post_blocks")
    policy.require(lambda ctx: False, on_fail=BLOCK, reason="result rejected", hook="post")

    with pytest.raises(GuardBlocked) as excinfo:
        evaluate_call(
            tool_name="delete_thing",
            args={},
            invoke=lambda: action({}),
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
            reversible=action,
        )

    assert excinfo.value.decision.undo_executed is False
    assert "NOT reverted" in excinfo.value.decision.reason

    event = ActionLedger.current().events()[-1]
    assert event.undo_op is None
    assert "no undo_fn configured" in event.reason


def test_post_block_with_undo_fn_still_records_the_undo():
    undone = []
    action = ReversibleAction(
        do_fn=lambda a: {"deleted": True},
        undo_fn=lambda a, s: undone.append(a),
        name="delete_thing",
        irreversibility_level="low",
    )
    policy = PolicySet("post_blocks")
    policy.require(lambda ctx: False, on_fail=BLOCK, reason="result rejected", hook="post")

    with pytest.raises(GuardBlocked):
        evaluate_call(
            tool_name="delete_thing",
            args={"id": 1},
            invoke=lambda: action({"id": 1}),
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
            reversible=action,
        )

    assert undone == [{"id": 1}]
    assert ActionLedger.current().events()[-1].undo_op == "delete_thing.undo"


# --- ReversibleAction through CharterInterceptor -------------------------
#
# `@guard` and the interceptor build the `invoke` closure differently — the
# interceptor passes the assembled `args` mapping to `action(args)` itself —
# so the reversible path has to be exercised through both entry points.


def test_interceptor_call_passes_args_mapping_to_the_action():
    received = []
    action = ReversibleAction(
        do_fn=lambda args: received.append(args) or {"ok": True},
        undo_fn=lambda args, snapshot: None,
        name="update_rows",
        irreversibility_level="low",
    )
    interceptor = CharterInterceptor(policies=[])

    assert interceptor.call("update_rows", action, args={"rows": 3}) == {"ok": True}
    assert received == [{"rows": 3}]


def test_interceptor_post_block_triggers_undo():
    undone = []
    action = ReversibleAction(
        do_fn=lambda args: {"rows_affected": args["rows"]},
        undo_fn=lambda args, snapshot: undone.append((args, snapshot)),
        name="update_rows",
        irreversibility_level="low",
        pre_snapshot=lambda args: {"before": True},
    )
    interceptor = CharterInterceptor(policies=[_post_block_policy()])

    with pytest.raises(GuardBlocked) as excinfo:
        interceptor.call("update_rows", action, args={"rows": 5})

    assert excinfo.value.decision.undo_executed is True
    assert undone == [({"rows": 5}, {"before": True})]
    assert ActionLedger.current().events()[-1].undo_op == "update_rows.undo"


def test_interceptor_blocks_a_permanent_action():
    calls = []
    action = ReversibleAction(
        do_fn=lambda args: calls.append(args), undo_fn=None, name="drop_db", irreversibility_level="permanent"
    )
    interceptor = CharterInterceptor(policies=[])

    with pytest.raises(GuardBlocked):
        interceptor.call("drop_db", action, args={})

    assert calls == []
    event = ActionLedger.current().events()[-1]
    assert event.decision == "BLOCK"
    assert event.policy == "reversible_action.permanent"


def test_interceptor_escalates_a_high_action_and_fails_safe_to_block():
    calls = []
    action = ReversibleAction(
        do_fn=lambda args: calls.append(args),
        undo_fn=None,
        name="delete_bucket",
        irreversibility_level="high",
        escalate_to="unregistered-scheme://ops",
    )
    interceptor = CharterInterceptor(policies=[])

    with pytest.raises(GuardBlocked):
        interceptor.call("delete_bucket", action, args={"bucket": "b"})

    assert calls == []
    event = ActionLedger.current().events()[-1]
    assert event.decision == "ESCALATE"
    assert event.policy == "reversible_action.high"


def test_interceptor_high_action_runs_once_the_escalation_is_approved():
    class Approve(EscalationHandler):
        def escalate(self, ctx, rule_result):
            return True

    register_handler("interceptor-approve-scheme", Approve())

    calls = []
    action = ReversibleAction(
        do_fn=lambda args: calls.append(args),
        undo_fn=None,
        name="delete_bucket",
        irreversibility_level="high",
        escalate_to="interceptor-approve-scheme://ops",
    )
    interceptor = CharterInterceptor(policies=[])

    interceptor.call("delete_bucket", action, args={"bucket": "b"})

    assert calls == [{"bucket": "b"}]


def test_interceptor_records_the_ledger_event_with_caller_identity():
    """The interceptor's `ExecutionScope` must reach the reversible action's
    intrinsic decision, not just the ordinary policy path."""
    action = ReversibleAction(
        do_fn=lambda args: None, undo_fn=None, name="drop_db", irreversibility_level="permanent"
    )
    interceptor = CharterInterceptor(policies=[], agent_id="cleanup_agent")

    with pytest.raises(GuardBlocked):
        interceptor.call("drop_db", action, args={"db": "prod"}, session_id="s1")

    event = ActionLedger.current().events()[-1]
    assert event.tool == "drop_db"
    assert event.args == {"db": "prod"}
    assert event.session_id == "s1"
    assert event.caller_agent_id == "cleanup_agent"
    assert event.delegation_chain == ["cleanup_agent"]


def test_interceptor_dry_run_never_undoes_a_reversible_action():
    undone = []
    action = ReversibleAction(
        do_fn=lambda args: {"rows_affected": args["rows"]},
        undo_fn=lambda args, snapshot: undone.append(args),
        name="update_rows",
        irreversibility_level="low",
    )
    interceptor = CharterInterceptor(policies=[_post_block_policy()], mode="dry_run")

    assert interceptor.call("update_rows", action, args={"rows": 5}) == {"rows_affected": 5}
    assert undone == []
    assert ActionLedger.current().events()[-1].decision == "BLOCK"


def test_wrap_tool_on_a_reversible_action_names_the_wrapper_after_the_tool():
    """`functools.update_wrapper` can't be used on a ReversibleAction — it has
    no `__name__` — so the wrapper takes the registered tool name instead."""
    action = ReversibleAction(
        do_fn=lambda args: args, undo_fn=None, name="update_rows", irreversibility_level="low"
    )
    interceptor = CharterInterceptor(policies=[])
    wrapped = interceptor.wrap_tool("update_rows_tool", action)

    assert wrapped.__name__ == "update_rows_tool"
    assert wrapped.__charter_tool_name__ == "update_rows_tool"
    assert wrapped(rows=1) == {"rows": 1}


async def test_acall_passes_args_mapping_to_an_async_action():
    received = []

    async def async_do(args):
        received.append(args)
        return {"ok": True}

    action = ReversibleAction(
        do_fn=async_do, undo_fn=lambda args, snapshot: None, name="update_rows", irreversibility_level="low"
    )
    interceptor = CharterInterceptor(policies=[])

    assert await interceptor.acall("update_rows", action, args={"rows": 3}) == {"ok": True}
    assert received == [{"rows": 3}]


async def test_acall_post_block_triggers_an_async_undo():
    undone = []

    async def async_do(args):
        return {"rows_affected": args["rows"]}

    async def async_undo(args, snapshot):
        undone.append((args, snapshot))

    action = ReversibleAction(
        do_fn=async_do,
        undo_fn=async_undo,
        name="update_rows",
        irreversibility_level="low",
        pre_snapshot=lambda args: {"before": True},
    )
    interceptor = CharterInterceptor(policies=[_post_block_policy()])

    with pytest.raises(GuardBlocked) as excinfo:
        await interceptor.acall("update_rows", action, args={"rows": 5})

    assert excinfo.value.decision.undo_executed is True
    assert undone == [({"rows": 5}, {"before": True})]
    assert ActionLedger.current().events()[-1].undo_op == "update_rows.undo"


async def test_acall_blocks_a_permanent_action_without_calling_do_fn():
    calls = []

    async def async_do(args):
        calls.append(args)

    action = ReversibleAction(do_fn=async_do, undo_fn=None, name="drop_db", irreversibility_level="permanent")
    interceptor = CharterInterceptor(policies=[])

    with pytest.raises(GuardBlocked):
        await interceptor.acall("drop_db", action, args={})

    assert calls == []
    assert ActionLedger.current().events()[-1].policy == "reversible_action.permanent"


async def test_acall_escalates_a_high_action_via_an_async_handler():
    class AsyncApprove(EscalationHandler):
        async def escalate(self, ctx, rule_result):
            return True

    register_handler("acall-approve-scheme", AsyncApprove())

    calls = []

    async def async_do(args):
        calls.append(args)

    action = ReversibleAction(
        do_fn=async_do,
        undo_fn=None,
        name="delete_bucket",
        irreversibility_level="high",
        escalate_to="acall-approve-scheme://ops",
    )
    interceptor = CharterInterceptor(policies=[])

    await interceptor.acall("delete_bucket", action, args={"bucket": "b"})

    assert calls == [{"bucket": "b"}]
    assert ActionLedger.current().events()[0].decision == "ESCALATE"


async def test_wrap_tool_on_an_async_reversible_action_is_awaitable():
    async def async_do(args):
        return args

    action = ReversibleAction(do_fn=async_do, undo_fn=None, name="update_rows", irreversibility_level="low")
    interceptor = CharterInterceptor(policies=[])
    wrapped = interceptor.wrap_tool("update_rows_tool", action)

    assert wrapped.__name__ == "update_rows_tool"
    assert await wrapped(rows=1) == {"rows": 1}
