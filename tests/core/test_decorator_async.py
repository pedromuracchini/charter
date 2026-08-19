import asyncio
import inspect

import pytest

from chokepoint.core.decorator import guard
from chokepoint.core.reversible import ReversibleAction
from chokepoint.decisions import ALLOW, BLOCK, ESCALATE, GuardBlocked
from chokepoint.ledger.ledger import ActionLedger


def _capturing_guard(seen):
    """A guard whose pre-hook records `ctx.args` and always passes."""

    def pre(ctx):
        seen.append(dict(ctx.args))
        return True

    return guard(pre=pre, on_fail=BLOCK, reason="never fires")


async def test_async_tool_allowed_and_blocked():
    calls = []

    @guard(pre=lambda ctx: ctx.args["amount"] < 500, on_fail=BLOCK, reason="too large")
    async def transfer(amount):
        calls.append(amount)
        await asyncio.sleep(0)
        return {"transferred": amount}

    assert inspect.iscoroutinefunction(transfer)
    assert await transfer(amount=100) == {"transferred": 100}
    with pytest.raises(GuardBlocked):
        await transfer(amount=1000)
    assert calls == [100]


async def test_async_post_block_triggers_auto_undo_for_async_undo_fn():
    undone = []

    async def async_do(args):
        await asyncio.sleep(0)
        return {"rows_affected": args["rows"]}

    async def async_undo(args, snapshot):
        await asyncio.sleep(0)
        undone.append((args, snapshot))

    action = ReversibleAction(
        do_fn=async_do,
        undo_fn=async_undo,
        name="update_rows",
        irreversibility_level="low",
        pre_snapshot=lambda args: {"before": True},
    )
    wrapped = guard(post=lambda ctx: ctx.result["rows_affected"] == 1, on_fail=BLOCK, reason="too many")(
        action
    )

    assert await wrapped(rows=1) == {"rows_affected": 1}
    with pytest.raises(GuardBlocked) as exc_info:
        await wrapped(rows=5)
    assert exc_info.value.decision.undo_executed is True
    assert undone == [({"rows": 5}, {"before": True})]


async def test_async_on_fail_allow_logs_but_never_blocks():
    ActionLedger.reset()

    @guard(pre=lambda ctx: ctx.args["x"] > 0, on_fail=ALLOW, reason="advisory")
    async def do_thing(x):
        return x

    assert await do_thing(x=-1) == -1
    events = ActionLedger.current().events()
    assert any(e.decision == "ALLOW" and e.policy is not None for e in events)


async def test_async_escalate_denied_by_default_blocks():
    @guard(
        pre=lambda ctx: False, on_fail=ESCALATE, reason="needs approval", escalate_to="slack://unconfigured"
    )
    async def risky():
        return "done"

    with pytest.raises(GuardBlocked):
        await risky()


async def test_async_positional_arguments_are_bound_into_ctx_args():
    seen = []

    @_capturing_guard(seen)
    async def transfer(amount, currency):
        return (amount, currency)

    assert await transfer(100, "EUR") == (100, "EUR")
    assert seen == [{"amount": 100, "currency": "EUR"}]


async def test_async_mixed_positional_and_keyword_arguments_are_bound():
    seen = []

    @_capturing_guard(seen)
    async def transfer(amount, currency):
        return (amount, currency)

    assert await transfer(100, currency="EUR") == (100, "EUR")
    assert seen == [{"amount": 100, "currency": "EUR"}]


async def test_async_keyword_only_parameters_are_bound():
    seen = []

    @_capturing_guard(seen)
    async def transfer(amount, *, currency):
        return (amount, currency)

    assert await transfer(100, currency="EUR") == (100, "EUR")
    assert seen == [{"amount": 100, "currency": "EUR"}]


async def test_async_guarded_function_keeps_its_original_signature():
    async def transfer(amount: int, currency: str = "USD") -> tuple:
        return (amount, currency)

    original = inspect.signature(transfer)
    guarded = _capturing_guard([])(transfer)

    assert inspect.signature(guarded) == original


async def test_async_var_keyword_parameter_is_flattened_into_ctx_args():
    seen = []

    @_capturing_guard(seen)
    async def call_api(endpoint, **extra):
        return (endpoint, extra)

    assert await call_api("/v1", retries=3) == ("/v1", {"retries": 3})
    assert seen == [{"endpoint": "/v1", "retries": 3}]


async def test_async_positional_only_parameter_still_works():
    seen = []

    @_capturing_guard(seen)
    async def hash_it(x, /):
        return x * 2

    assert await hash_it(21) == 42
    assert seen == [{"x": 21}]


async def test_async_defaults_are_not_injected_into_ctx_args():
    seen = []

    @_capturing_guard(seen)
    async def transfer(amount, currency="USD"):
        return (amount, currency)

    assert await transfer(100) == (100, "USD")
    assert seen == [{"amount": 100}]


async def test_async_predicate_can_reject_a_positionally_passed_argument():
    @guard(pre=lambda ctx: ctx.args["amount"] < 500, on_fail=BLOCK, reason="too large")
    async def transfer(amount):
        return {"ok": True}

    assert await transfer(100) == {"ok": True}
    with pytest.raises(GuardBlocked):
        await transfer(1000)


async def test_async_binding_a_bad_call_raises_typeerror_before_any_policy_runs():
    seen = []

    @_capturing_guard(seen)
    async def transfer(amount):
        return amount

    with pytest.raises(TypeError):
        await transfer(1, 2)
    assert seen == []


async def test_async_reversible_action_still_rejects_positional_arguments():
    async def do_fn(args):
        return args

    action = ReversibleAction(do_fn=do_fn, undo_fn=None, name="update_rows", irreversibility_level="low")
    wrapped = guard(pre=lambda ctx: True, on_fail=BLOCK, reason="never")(action)

    assert await wrapped(rows=1) == {"rows": 1}
    with pytest.raises(TypeError, match="keyword arguments only"):
        await wrapped({"rows": 1})
