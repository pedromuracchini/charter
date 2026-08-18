import asyncio
import time
import warnings

import pytest

from charter._engine import (
    _run_with_timeout,
    _run_with_timeout_async,
    evaluate_call,
    evaluate_call_async,
)
from charter._scope import ExecutionScope, current_scope, use_scope
from charter.core.context import GuardContext
from charter.core.escalation import (
    EscalationHandler,
    FailSafeEscalationHandler,
    register_handler,
    resolve_handler,
)
from charter.core.policy_set import PolicySet
from charter.core.reversible import ReversibleAction
from charter.decisions import ESCALATE, GuardBlocked, RuleResult
from charter.errors import ConfigurationError, ConfigurationWarning
from charter.ledger.ledger import ActionLedger


def _ctx():
    return GuardContext.build(tool_name="t", args={}, scope=ExecutionScope())


def _rule_result(**overrides):
    base = {"passed": False, "on_fail": ESCALATE, "reason": "r", "policy_name": "p"}
    base.update(overrides)
    return RuleResult(**base)


def test_default_handler_denies():
    handler = resolve_handler(None)
    assert isinstance(handler, FailSafeEscalationHandler)
    assert handler.escalate(_ctx(), _rule_result()) is False


def test_resolve_handler_uses_registered_scheme():
    class AlwaysApprove(EscalationHandler):
        def escalate(self, ctx, rule_result):
            return True

    register_handler("test-scheme", AlwaysApprove())
    handler = resolve_handler("test-scheme://wherever")
    assert isinstance(handler, AlwaysApprove)
    assert handler.escalate(_ctx(), _rule_result()) is True


def test_unresolved_scheme_falls_back_to_default():
    handler = resolve_handler("unregistered-scheme://x")
    assert isinstance(handler, FailSafeEscalationHandler)


def test_run_with_timeout_denies_a_hung_handler_promptly():
    def hangs_forever():
        time.sleep(5)
        return True

    start = time.perf_counter()
    result = _run_with_timeout(hangs_forever, timeout_s=0.1)
    elapsed = time.perf_counter() - start

    assert result is False
    assert elapsed < 1.0  # bounded by timeout_s, not the handler's 5s sleep


def test_run_with_timeout_denies_on_exception():
    def broken():
        raise RuntimeError("boom")

    assert _run_with_timeout(broken, timeout_s=1.0) is False


def test_run_with_timeout_returns_handler_result_when_fast_enough():
    assert _run_with_timeout(lambda: True, timeout_s=1.0) is True
    assert _run_with_timeout(lambda: False, timeout_s=1.0) is False


def test_evaluate_call_enforces_timeout_end_to_end():
    class SlowHandler(EscalationHandler):
        def escalate(self, ctx, rule_result):
            time.sleep(5)
            return True

    register_handler("slow-test-scheme", SlowHandler())

    policy = PolicySet("needs_approval")
    policy.require(
        lambda ctx: False,
        on_fail=ESCALATE,
        reason="needs approval",
        escalate_to="slow-test-scheme://wherever",
        timeout_s=1,
    )

    start = time.perf_counter()
    with pytest.raises(GuardBlocked):
        evaluate_call(
            tool_name="t",
            args={},
            invoke=lambda: {"ok": True},
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )
    elapsed = time.perf_counter() - start
    assert elapsed < 4.0  # bounded by timeout_s=1, not the handler's 5s sleep


def test_sync_engine_denies_an_async_handler_instead_of_silently_approving():
    """`bool(coroutine)` is always True — an accidentally-async handler used
    via the sync engine must be denied explicitly, not silently approved."""

    async def async_escalate():
        return True

    assert _run_with_timeout(async_escalate, timeout_s=1.0) is False


async def test_run_with_timeout_async_denies_a_hung_sync_handler_promptly():
    def hangs_forever(ctx, rule_result):
        time.sleep(5)
        return True

    start = time.perf_counter()
    result = await _run_with_timeout_async(hangs_forever, _ctx(), _rule_result(timeout_s=0.1))
    elapsed = time.perf_counter() - start

    assert result is False
    assert elapsed < 1.0


async def test_run_with_timeout_async_denies_a_hung_async_handler_promptly():
    async def hangs_forever(ctx, rule_result):
        await asyncio.sleep(5)
        return True

    start = time.perf_counter()
    result = await _run_with_timeout_async(hangs_forever, _ctx(), _rule_result(timeout_s=0.1))
    elapsed = time.perf_counter() - start

    assert result is False
    assert elapsed < 1.0


async def test_run_with_timeout_async_returns_result_for_both_sync_and_async_handlers():
    def sync_handler(ctx, rule_result):
        return True

    async def async_handler(ctx, rule_result):
        return False

    assert await _run_with_timeout_async(sync_handler, _ctx(), _rule_result(timeout_s=1.0)) is True
    assert await _run_with_timeout_async(async_handler, _ctx(), _rule_result(timeout_s=1.0)) is False


async def test_evaluate_call_async_enforces_timeout_end_to_end():
    class SlowAsyncHandler(EscalationHandler):
        async def escalate(self, ctx, rule_result):
            await asyncio.sleep(5)
            return True

    register_handler("slow-async-test-scheme", SlowAsyncHandler())

    policy = PolicySet("needs_approval_async")
    policy.require(
        lambda ctx: False,
        on_fail=ESCALATE,
        reason="needs approval",
        escalate_to="slow-async-test-scheme://wherever",
        timeout_s=1,
    )

    async def invoke():
        return {"ok": True}

    start = time.perf_counter()
    with pytest.raises(GuardBlocked):
        await evaluate_call_async(
            tool_name="t",
            args={},
            invoke=invoke,
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )
    elapsed = time.perf_counter() - start
    assert elapsed < 4.0


class _SpyHandler(EscalationHandler):
    """Records every invocation so a test can assert it was never contacted."""

    def __init__(self, approves: bool = True) -> None:
        self.calls: list[str] = []
        self._approves = approves

    def escalate(self, ctx, rule_result):
        self.calls.append(ctx.tool_name)
        return self._approves


def _escalating_policy(name: str, scheme: str) -> PolicySet:
    policy = PolicySet(name)
    policy.require(
        lambda ctx: False,
        on_fail=ESCALATE,
        reason="needs approval",
        escalate_to=f"{scheme}://wherever",
    )
    return policy


@pytest.mark.parametrize("mode", ["dry_run", "observe"])
def test_non_enforce_modes_never_contact_an_escalation_handler(mode):
    """A dry run must answer "what would this policy do?" without paging a
    human — no Slack post, no approval webhook, no blocking on input()."""
    spy = _SpyHandler()
    register_handler("spy-scheme", spy)

    result = evaluate_call(
        tool_name="t",
        args={},
        invoke=lambda: {"ok": True},
        policies=[_escalating_policy("dry", "spy-scheme")],
        mode=mode,
        scope=current_scope(),
    )

    assert spy.calls == []
    assert result == {"ok": True}  # non-enforce modes never block


@pytest.mark.parametrize("mode", ["dry_run", "observe"])
async def test_non_enforce_modes_never_contact_a_handler_async(mode):
    spy = _SpyHandler()
    register_handler("spy-async-scheme", spy)

    async def invoke():
        return {"ok": True}

    result = await evaluate_call_async(
        tool_name="t",
        args={},
        invoke=invoke,
        policies=[_escalating_policy("dry_async", "spy-async-scheme")],
        mode=mode,
        scope=current_scope(),
    )

    assert spy.calls == []
    assert result == {"ok": True}


def test_dry_run_still_records_the_escalation_it_would_have_raised():
    """Not contacting the handler must not cost the audit trail — the whole
    point of a dry run is seeing what the policy would have done."""
    register_handler("spy-recorded-scheme", _SpyHandler())

    evaluate_call(
        tool_name="risky_tool",
        args={},
        invoke=lambda: None,
        policies=[_escalating_policy("recorded", "spy-recorded-scheme")],
        mode="dry_run",
        scope=current_scope(),
    )

    events = [e for e in ActionLedger.current().events() if e.tool == "risky_tool"]
    assert len(events) == 1
    assert events[0].decision == "ESCALATE"
    assert events[0].mode == "dry_run"
    assert "not resolved" in events[0].reason


def test_enforce_mode_still_contacts_the_handler():
    spy = _SpyHandler(approves=True)
    register_handler("spy-enforce-scheme", spy)

    evaluate_call(
        tool_name="t",
        args={},
        invoke=lambda: {"ok": True},
        policies=[_escalating_policy("enforced", "spy-enforce-scheme")],
        mode="enforce",
        scope=current_scope(),
    )

    assert spy.calls == ["t"]


def test_register_handler_rejects_a_scheme_that_is_itself_a_uri():
    """`resolve_handler` matches on `urlsplit(target).scheme`, so registering
    'slack://x' would never match anything."""
    with pytest.raises(ConfigurationError, match="bare URI scheme"):
        register_handler("slack://x", _SpyHandler())


def test_register_handler_rejects_an_empty_or_colon_bearing_scheme():
    with pytest.raises(ConfigurationError):
        register_handler("", _SpyHandler())
    with pytest.raises(ConfigurationError):
        register_handler("slack:", _SpyHandler())


def test_schemeless_escalate_to_on_a_policy_warns():
    """A plain 'security-team' has an empty scheme, matches no handler, and
    silently blocks every guarded call forever."""
    policy = PolicySet("needs_approval")
    with pytest.warns(ConfigurationWarning, match="no URI scheme"):
        policy.require(
            lambda ctx: False,
            on_fail=ESCALATE,
            reason="needs approval",
            escalate_to="security-team",
        )


def test_schemeless_escalate_to_on_a_high_reversible_action_warns():
    with pytest.warns(ConfigurationWarning, match="no URI scheme"):
        ReversibleAction(
            do_fn=lambda args: None,
            undo_fn=None,
            name="delete_bucket",
            irreversibility_level="high",
            escalate_to="security-team",
        )


def test_a_scheme_bearing_escalate_to_does_not_warn():
    policy = PolicySet("needs_approval")
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigurationWarning)
        policy.require(
            lambda ctx: False,
            on_fail=ESCALATE,
            reason="needs approval",
            escalate_to="slack://security-team",
        )


class _ScopeCapturingHandler(EscalationHandler):
    """Records the `ExecutionScope` visible from inside the handler."""

    def __init__(self) -> None:
        self.scopes = []

    def escalate(self, ctx, rule_result):
        self.scopes.append(current_scope())
        return True


class _AsyncScopeCapturingHandler(EscalationHandler):
    def __init__(self) -> None:
        self.scopes = []

    async def escalate(self, ctx, rule_result):
        self.scopes.append(current_scope())
        return True


def test_handler_sees_the_callers_scope_under_the_sync_engine():
    """The sync engine runs the handler in a worker thread, which starts with a
    fresh, empty contextvars context unless the caller's is copied in — a
    handler routing an approval by caller identity would otherwise see the
    root scope."""
    handler = _ScopeCapturingHandler()
    register_handler("scope-sync-scheme", handler)

    scope = ExecutionScope(session_id="s1", caller_agent_id="billing_agent", caller_role="finance")
    with use_scope(scope):
        evaluate_call(
            tool_name="t",
            args={},
            invoke=lambda: {"ok": True},
            policies=[_escalating_policy("scoped", "scope-sync-scheme")],
            mode="enforce",
            scope=scope,
        )

    assert [(s.session_id, s.caller_agent_id, s.caller_role) for s in handler.scopes] == [
        ("s1", "billing_agent", "finance")
    ]


async def test_sync_handler_sees_the_callers_scope_under_the_async_engine():
    handler = _ScopeCapturingHandler()
    register_handler("scope-async-sync-scheme", handler)

    async def invoke():
        return {"ok": True}

    scope = ExecutionScope(session_id="s2", caller_agent_id="billing_agent", caller_role="finance")
    with use_scope(scope):
        await evaluate_call_async(
            tool_name="t",
            args={},
            invoke=invoke,
            policies=[_escalating_policy("scoped_async", "scope-async-sync-scheme")],
            mode="enforce",
            scope=scope,
        )

    assert [(s.session_id, s.caller_role) for s in handler.scopes] == [("s2", "finance")]


async def test_async_handler_sees_the_callers_scope_under_the_async_engine():
    handler = _AsyncScopeCapturingHandler()
    register_handler("scope-async-scheme", handler)

    async def invoke():
        return {"ok": True}

    scope = ExecutionScope(session_id="s3", caller_agent_id="billing_agent", caller_role="finance")
    with use_scope(scope):
        await evaluate_call_async(
            tool_name="t",
            args={},
            invoke=invoke,
            policies=[_escalating_policy("scoped_async2", "scope-async-scheme")],
            mode="enforce",
            scope=scope,
        )

    assert [(s.session_id, s.caller_role) for s in handler.scopes] == [("s3", "finance")]


async def test_async_engine_denies_a_hung_async_handler_end_to_end_within_the_timeout():
    """The async twin of `test_evaluate_call_enforces_timeout_end_to_end`: an
    `async def escalate` that never answers must be denied on the deadline, not
    awaited to completion."""

    class HangingAsyncHandler(EscalationHandler):
        async def escalate(self, ctx, rule_result):
            await asyncio.sleep(30)
            return True

    register_handler("hung-async-scheme", HangingAsyncHandler())

    policy = PolicySet("hung_async")
    policy.require(
        lambda ctx: False,
        on_fail=ESCALATE,
        reason="needs approval",
        escalate_to="hung-async-scheme://wherever",
        timeout_s=0.2,
    )

    async def invoke():
        return {"ok": True}

    start = time.perf_counter()
    with pytest.raises(GuardBlocked) as excinfo:
        await evaluate_call_async(
            tool_name="t",
            args={},
            invoke=invoke,
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )
    elapsed = time.perf_counter() - start

    assert elapsed < 5.0  # bounded by timeout_s=0.2, not the handler's 30s sleep
    assert "denied/timed out" in excinfo.value.decision.reason


async def test_a_hung_sync_handler_does_not_block_the_event_loop():
    """A sync `escalate` is dispatched to a thread pool precisely so
    `asyncio.wait_for`'s timeout can still fire — awaited naively it would
    freeze the loop and the deadline could never be reached."""
    ticks = []

    async def ticker():
        for _ in range(20):
            await asyncio.sleep(0.01)
            ticks.append(1)

    def hangs_forever(ctx, rule_result):
        time.sleep(5)
        return True

    task = asyncio.ensure_future(ticker())
    result = await _run_with_timeout_async(hangs_forever, _ctx(), _rule_result(timeout_s=0.2))
    task.cancel()

    assert result is False
    assert ticks, "the event loop kept running while the handler was blocked"
