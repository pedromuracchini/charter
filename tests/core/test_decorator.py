import inspect

import pytest

from tollgate.core.decorator import guard
from tollgate.core.reversible import ReversibleAction
from tollgate.decisions import ALLOW, BLOCK, ESCALATE, GuardBlocked
from tollgate.ledger.ledger import ActionLedger


def _capturing_guard(seen):
    """A guard whose pre-hook records `ctx.args` and always passes."""

    def pre(ctx):
        seen.append(dict(ctx.args))
        return True

    return guard(pre=pre, on_fail=BLOCK, reason="never fires")


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
    @guard(
        pre=lambda ctx: False, on_fail=ESCALATE, reason="needs approval", escalate_to="slack://unconfigured"
    )
    def risky():
        return "done"

    with pytest.raises(GuardBlocked):
        risky()


def test_guard_requires_pre_or_post():
    with pytest.raises(ValueError):
        guard(on_fail=BLOCK, reason="x")


def test_positional_arguments_are_bound_into_ctx_args():
    seen = []

    @_capturing_guard(seen)
    def transfer(amount, currency):
        return (amount, currency)

    assert transfer(100, "EUR") == (100, "EUR")
    assert seen == [{"amount": 100, "currency": "EUR"}]


def test_mixed_positional_and_keyword_arguments_are_bound():
    seen = []

    @_capturing_guard(seen)
    def transfer(amount, currency):
        return (amount, currency)

    assert transfer(100, currency="EUR") == (100, "EUR")
    assert seen == [{"amount": 100, "currency": "EUR"}]


def test_keyword_only_parameters_are_bound():
    seen = []

    @_capturing_guard(seen)
    def transfer(amount, *, currency):
        return (amount, currency)

    assert transfer(100, currency="EUR") == (100, "EUR")
    assert seen == [{"amount": 100, "currency": "EUR"}]


def test_var_positional_is_bound_under_its_own_name():
    seen = []

    @_capturing_guard(seen)
    def collect(first, *rest):
        return (first, rest)

    assert collect(1, 2, 3) == (1, (2, 3))
    assert seen == [{"first": 1, "rest": (2, 3)}]


def test_guarded_function_keeps_its_original_signature():
    """Every framework that builds a JSON schema from a tool — LangChain, the
    OpenAI Agents SDK, MCP — reads this signature and emits positional calls
    from it."""

    def transfer(amount: int, currency: str = "USD") -> tuple:
        return (amount, currency)

    original = inspect.signature(transfer)
    guarded = _capturing_guard([])(transfer)

    assert inspect.signature(guarded) == original


def test_var_keyword_parameter_is_flattened_into_ctx_args():
    """A predicate reading ctx.args["x"] shouldn't have to care whether the
    tool declared `x` explicitly or swept it up in `**extra`."""
    seen = []

    @_capturing_guard(seen)
    def call_api(endpoint, **extra):
        return (endpoint, extra)

    assert call_api("/v1", retries=3, verbose=True) == ("/v1", {"retries": 3, "verbose": True})
    assert seen == [{"endpoint": "/v1", "retries": 3, "verbose": True}]


def test_positional_only_parameter_still_works():
    seen = []

    @_capturing_guard(seen)
    def hash_it(x, /):
        return x * 2

    assert hash_it(21) == 42
    assert seen == [{"x": 21}]


def test_defaults_are_not_injected_into_ctx_args():
    """A predicate written to check whether an argument was supplied at all
    must keep seeing it absent."""
    seen = []

    @_capturing_guard(seen)
    def transfer(amount, currency="USD"):
        return (amount, currency)

    assert transfer(100) == (100, "USD")
    assert seen == [{"amount": 100}]
    assert "currency" not in seen[0]


def test_a_predicate_can_reject_a_positionally_passed_argument():
    @guard(pre=lambda ctx: ctx.args["amount"] < 500, on_fail=BLOCK, reason="too large")
    def transfer(amount):
        return {"ok": True}

    assert transfer(100) == {"ok": True}
    with pytest.raises(GuardBlocked):
        transfer(1000)


def test_binding_a_bad_call_raises_typeerror_before_any_policy_runs():
    seen = []

    @_capturing_guard(seen)
    def transfer(amount):
        return amount

    with pytest.raises(TypeError):
        transfer(1, 2)
    assert seen == []


def test_reversible_action_still_rejects_positional_arguments():
    """A ReversibleAction takes a single args mapping, so it has no signature
    to bind against and stays keyword-only."""
    action = ReversibleAction(
        do_fn=lambda args: args, undo_fn=None, name="update_rows", irreversibility_level="low"
    )
    wrapped = guard(pre=lambda ctx: True, on_fail=BLOCK, reason="never")(action)

    assert wrapped(rows=1) == {"rows": 1}
    with pytest.raises(TypeError, match="keyword arguments only"):
        wrapped({"rows": 1})


def test_uninspectable_callable_stays_keyword_only():
    """`time.time` has no introspectable signature, so `_signature_of` returns
    None and the wrapper falls back to keyword-only rather than crashing."""
    import time

    wrapped = guard(pre=lambda ctx: True, on_fail=BLOCK, reason="never")(time.time)

    assert isinstance(wrapped(), float)
    with pytest.raises(TypeError, match="keyword arguments only"):
        wrapped(1)


def test_builtin_with_a_signature_binds_positionally():
    seen = []
    wrapped = _capturing_guard(seen)(len)

    assert wrapped([1, 2, 3]) == 3
    assert seen == [{"obj": [1, 2, 3]}]
