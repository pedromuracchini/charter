import asyncio
import time

import pytest

from tollgate._engine import (
    _run_with_timeout,
    _run_with_timeout_async,
    evaluate_call,
    evaluate_call_async,
)
from tollgate._scope import ExecutionScope, current_scope
from tollgate.core.context import GuardContext
from tollgate.core.escalation import (
    EscalationHandler,
    FailSafeEscalationHandler,
    register_handler,
    resolve_handler,
)
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import ESCALATE, GuardBlocked, RuleResult


def _ctx():
    return GuardContext.build(tool_name="t", args={}, scope=ExecutionScope())


def _rule_result(**overrides):
    base = dict(passed=False, on_fail=ESCALATE, reason="r", policy_name="p")
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
