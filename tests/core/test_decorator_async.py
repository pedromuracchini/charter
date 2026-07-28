import asyncio

import pytest

from tollgate.core.decorator import guard
from tollgate.core.reversible import ReversibleAction
from tollgate.decisions import ALLOW, BLOCK, ESCALATE, GuardBlocked
from tollgate.ledger.ledger import ActionLedger


async def test_async_tool_allowed_and_blocked():
    calls = []

    @guard(pre=lambda ctx: ctx.args["amount"] < 500, on_fail=BLOCK, reason="too large")
    async def transfer(amount):
        calls.append(amount)
        await asyncio.sleep(0)
        return {"transferred": amount}

    assert asyncio.iscoroutinefunction(transfer)
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
    @guard(pre=lambda ctx: False, on_fail=ESCALATE, reason="needs approval", escalate_to="slack://unconfigured")
    async def risky():
        return "done"

    with pytest.raises(GuardBlocked):
        await risky()
