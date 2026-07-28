import pytest

from tollgate.core.decorator import guard
from tollgate.core.reversible import ReversibleAction
from tollgate.decisions import ALLOW, BLOCK, ESCALATE, GuardBlocked
from tollgate.ledger.ledger import ActionLedger


def test_pre_block_prevents_execution():
    calls = []

    @guard(pre=lambda ctx: ctx.args["amount"] < 500, on_fail=BLOCK, reason="too large")
    def transfer(amount):
        calls.append(amount)
        return {"ok": True}

    assert transfer(amount=100) == {"ok": True}
    with pytest.raises(GuardBlocked):
        transfer(amount=1000)
    assert calls == [100]


def test_post_block_raises_after_the_call_already_ran():
    @guard(post=lambda ctx: ctx.result["rows_affected"] == 1, on_fail=BLOCK, reason="too many rows")
    def update(rows_affected):
        return {"rows_affected": rows_affected}

    assert update(rows_affected=1) == {"rows_affected": 1}
    with pytest.raises(GuardBlocked):
        update(rows_affected=5)


def test_post_block_triggers_auto_undo_for_reversible_action():
    undone = []
    action = ReversibleAction(
        do_fn=lambda args: {"rows_affected": args["rows"]},
        undo_fn=lambda args, snapshot: undone.append((args, snapshot)),
        name="update_rows",
        irreversibility_level="low",
        pre_snapshot=lambda args: {"before": True},
    )
    def post_check(ctx):
        return ctx.result["rows_affected"] == 1

    wrapped = guard(post=post_check, on_fail=BLOCK, reason="too many rows")(action)

    assert wrapped(rows=1) == {"rows_affected": 1}
    with pytest.raises(GuardBlocked) as exc_info:
        wrapped(rows=5)
    assert exc_info.value.decision.undo_executed is True
    assert undone == [({"rows": 5}, {"before": True})]


def test_on_fail_allow_logs_but_never_blocks():
    @guard(pre=lambda ctx: ctx.args["x"] > 0, on_fail=ALLOW, reason="x should be positive (advisory)")
    def do_thing(x):
        return x

    assert do_thing(x=-1) == -1
    events = ActionLedger.current().events()
    assert any(e.decision == "ALLOW" and e.policy is not None for e in events)


def test_escalate_denied_by_default_blocks():
    @guard(pre=lambda ctx: False, on_fail=ESCALATE, reason="needs approval", escalate_to="slack://unconfigured")
    def risky():
        return "done"

    with pytest.raises(GuardBlocked):
        risky()


def test_guard_requires_pre_or_post():
    with pytest.raises(ValueError):
        guard(on_fail=BLOCK, reason="x")
