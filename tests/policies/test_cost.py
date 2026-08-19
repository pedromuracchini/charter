"""Token and cost policies for LLM tool calls."""

import pytest

from chokepoint.core.interceptor import ChokepointInterceptor
from chokepoint.decisions import ESCALATE, GuardBlocked
from chokepoint.policies import (
    budget_policy,
    extract_usage,
    token_budget_policy,
    token_cost,
    token_count,
    token_limit_policy,
)


def _llm(input_tokens=1000, output_tokens=100):
    """A tool shaped like an Anthropic/OpenAI response."""

    def call_llm(**kwargs):
        return {"text": "...", "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}

    return call_llm


# --- usage extraction ------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        {"usage": {"input_tokens": 30, "output_tokens": 7}},
        {"usage": {"prompt_tokens": 30, "completion_tokens": 7}},
        {"usageMetadata": {"promptTokenCount": 30, "candidatesTokenCount": 7}},
        {"input_tokens": 30, "output_tokens": 7},
    ],
)
def test_usage_is_found_across_provider_shapes(result):
    assert extract_usage(result) == (30, 7)


def test_usage_is_read_off_objects_as_well_as_dicts():
    """An SDK returns a model; the same response through JSON is a dict."""

    class Usage:
        input_tokens = 30
        output_tokens = 7

    class Response:
        usage = Usage()

    assert extract_usage(Response()) == (30, 7)


@pytest.mark.parametrize("result", [None, {}, {"text": "no usage here"}, "a string"])
def test_a_response_without_usage_contributes_zero_rather_than_failing(result):
    """A tool with no usage block must not fail-closed every call."""
    assert extract_usage(result) == (0, 0)
    assert token_count(result) == 0


def test_token_count_sums_both_directions():
    assert token_count({"usage": {"input_tokens": 1000, "output_tokens": 250}}) == 1250


# --- pricing ---------------------------------------------------------------


def test_token_cost_prices_per_million_by_default():
    from chokepoint._scope import ExecutionScope
    from chokepoint.core.context import GuardContext

    ctx = GuardContext.build(tool_name="t", args={}, scope=ExecutionScope())
    ctx.result = {"usage": {"input_tokens": 1_000_000, "output_tokens": 1_000_000}}

    assert token_cost(3.0, 15.0)(ctx) == pytest.approx(18.0)


def test_token_cost_accepts_per_1k_pricing():
    from chokepoint._scope import ExecutionScope
    from chokepoint.core.context import GuardContext

    ctx = GuardContext.build(tool_name="t", args={}, scope=ExecutionScope())
    ctx.result = {"usage": {"input_tokens": 1000, "output_tokens": 1000}}

    assert token_cost(0.003, 0.015, per_tokens=1000)(ctx) == pytest.approx(0.018)


# --- the policies ----------------------------------------------------------


def test_token_budget_stops_the_session_once_spent():
    """$0.90 per call against a $2 cap: three run, the fourth is refused."""
    tool = _llm(input_tokens=200_000, output_tokens=20_000)
    policy = token_budget_policy(2.00, input_price=3.00, output_price=15.00, tool_name="call_llm")
    interceptor = ChokepointInterceptor(policies=[policy])

    for _ in range(3):
        interceptor.call("call_llm", tool, prompt="x")

    with pytest.raises(GuardBlocked, match="budget of 2 is exhausted"):
        interceptor.call("call_llm", tool, prompt="x")


def test_the_call_that_crosses_the_threshold_still_runs():
    """Documented behavior, not an accident: you cannot price an LLM call
    before making it, so the cap is 'stop once spent', not 'never exceed'."""
    calls = []

    def tool(**kwargs):
        calls.append(1)
        return {"usage": {"input_tokens": 1_000_000, "output_tokens": 0}}

    policy = token_budget_policy(1.00, input_price=10.00, output_price=0.0, tool_name="call_llm")
    interceptor = ChokepointInterceptor(policies=[policy])

    interceptor.call("call_llm", tool, prompt="x")  # spends 10.00 against a 1.00 cap
    assert calls == [1]

    with pytest.raises(GuardBlocked):
        interceptor.call("call_llm", tool, prompt="x")
    assert calls == [1]  # the second never reached the tool


def test_token_budget_is_per_session():
    tool = _llm(input_tokens=1_000_000, output_tokens=0)
    policy = token_budget_policy(1.00, input_price=10.00, output_price=0.0, tool_name="call_llm")
    interceptor = ChokepointInterceptor(policies=[policy])

    interceptor.call("call_llm", tool, session_id="a", prompt="x")
    with pytest.raises(GuardBlocked):
        interceptor.call("call_llm", tool, session_id="a", prompt="x")

    interceptor.call("call_llm", tool, session_id="b", prompt="x")  # unaffected


def test_token_budget_can_escalate_instead_of_blocking():
    """Often the better fit — a human decides if this run is worth more money."""
    tool = _llm(input_tokens=1_000_000, output_tokens=0)
    policy = token_budget_policy(
        1.00,
        input_price=10.00,
        output_price=0.0,
        tool_name="call_llm",
        on_fail=ESCALATE,
        escalate_to="unrouted://approvals",
    )
    interceptor = ChokepointInterceptor(policies=[policy])
    interceptor.call("call_llm", tool, prompt="x")

    with pytest.raises(GuardBlocked, match="escalation denied"):
        interceptor.call("call_llm", tool, prompt="x")


def test_token_limit_counts_tokens_not_money():
    tool = _llm(input_tokens=4000, output_tokens=1000)
    interceptor = ChokepointInterceptor(policies=[token_limit_policy(12_000, tool_name="call_llm")])

    interceptor.call("call_llm", tool, prompt="x")  # 5,000
    interceptor.call("call_llm", tool, prompt="x")  # 10,000
    interceptor.call("call_llm", tool, prompt="x")  # 15,000 — over

    with pytest.raises(GuardBlocked, match="token limit of 12,000"):
        interceptor.call("call_llm", tool, prompt="x")


def test_a_failing_llm_call_is_not_charged():
    """The post hook never runs when the tool raises — you weren't billed."""

    def broken(**kwargs):
        raise RuntimeError("upstream 500")

    policy = token_budget_policy(0.01, input_price=1000.0, output_price=0.0, tool_name="call_llm")
    interceptor = ChokepointInterceptor(policies=[policy])

    for _ in range(5):
        with pytest.raises(RuntimeError):
            interceptor.call("call_llm", broken, prompt="x")

    # Nothing was charged, so a working call still gets through.
    interceptor.call("call_llm", _llm(0, 0), prompt="x")


# --- budget_policy's two shapes --------------------------------------------


def test_budget_policy_requires_at_least_one_cost_function():
    with pytest.raises(ValueError, match="amount_from and/or actual_from"):
        budget_policy(100.0)


def test_actual_from_reads_the_result_which_pre_hooks_cannot_see():
    """This is the case that was broken: amount_from ran at the pre hook,
    where ctx.result is still None, so reading usage fail-closed to BLOCK."""
    policy = budget_policy(
        100.0,
        actual_from=lambda ctx: ctx.result["usage"]["output_tokens"],
        tool_name="call_llm",
    )
    interceptor = ChokepointInterceptor(policies=[policy])
    interceptor.call("call_llm", _llm(output_tokens=10), prompt="x")


def test_an_estimate_bounds_the_overshoot_and_the_actual_supersedes_it():
    """With both, the pre hook checks the estimate and the post hook charges
    the real figure — the estimate is never double-counted."""
    tool = _llm(input_tokens=0, output_tokens=100)
    policy = budget_policy(
        250.0,
        amount_from=lambda ctx: 10.0,  # a deliberate under-estimate
        actual_from=lambda ctx: float(ctx.result["usage"]["output_tokens"]),
        tool_name="call_llm",
    )
    interceptor = ChokepointInterceptor(policies=[policy])

    interceptor.call("call_llm", tool, prompt="x")  # charged 100, not 10 or 110
    interceptor.call("call_llm", tool, prompt="x")  # 200

    # 200 + the 10 estimate is under 250, so this one is admitted...
    interceptor.call("call_llm", tool, prompt="x")  # now 300
    with pytest.raises(GuardBlocked):
        interceptor.call("call_llm", tool, prompt="x")


def test_argument_based_budgets_are_unchanged():
    """The original shape has to keep working exactly as before."""
    policy = budget_policy(100.0, lambda ctx: ctx.args["amount"], tool_name="transfer")
    interceptor = ChokepointInterceptor(policies=[policy])

    interceptor.call("transfer", lambda **kw: None, amount=60.0)
    interceptor.call("transfer", lambda **kw: None, amount=30.0)
    with pytest.raises(GuardBlocked, match="would be exceeded"):
        interceptor.call("transfer", lambda **kw: None, amount=20.0)
