"""Token and cost policies for LLM tool calls.

The runaway-spend failure mode is specific to agents: a loop that retries,
re-reads its context and re-prompts can burn a month's budget in an afternoon,
and nothing in the tool call itself looks wrong. `rate_limit_policy` bounds the
*number* of calls, which is a blunt proxy — one call with a 200k-token context
costs more than a hundred short ones.

These read the `usage` block off the model's response, so they price what
actually happened rather than what was requested. That means they run in the
post hook: the call that crosses the budget still completes, and the next one
is refused. You cannot know an LLM call's price before making it, and a policy
that pretends otherwise would just be guessing.

**No pricing table ships here.** Rates change, differ by tier and region, and a
stale constant baked into a security library would silently mis-bill. Prices
are parameters, quoted per *million* tokens the way providers quote them:

    from chokepoint.policies import token_budget_policy

    # Stop this session once it has spent $5 on the model.
    token_budget_policy(5.00, input_price=3.00, output_price=15.00, tool_name="call_llm")
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from chokepoint.core.context import GuardContext
from chokepoint.core.policy_set import PolicySet
from chokepoint.decisions import BLOCK, Decision, Severity
from chokepoint.policies.limits import budget_policy

#: Where the usage block hides on a response. Checked in order.
USAGE_FIELDS: tuple[str, ...] = ("usage", "token_usage", "usageMetadata")

#: Input/prompt token field names across the providers people actually use.
#: Anthropic says `input_tokens`, OpenAI says `prompt_tokens`, Google says
#: `promptTokenCount`.
INPUT_TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "prompt_tokens",
    "promptTokenCount",
    "inputTokens",
)
OUTPUT_TOKEN_FIELDS: tuple[str, ...] = (
    "output_tokens",
    "completion_tokens",
    "candidatesTokenCount",
    "outputTokens",
)


def _get(obj: Any, name: str) -> Any:
    """Read `name` off a mapping *or* an object.

    SDK responses are pydantic models or dataclasses; the same response
    round-tripped through JSON is a dict. Handling both means one extractor
    works whether the tool returns the raw SDK object or a serialized copy.
    """
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _first(obj: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        value = _get(obj, name)
        if value is not None:
            return value
    return None


def extract_usage(result: Any) -> tuple[int, int]:
    """Pull `(input_tokens, output_tokens)` out of a model response.

    Accepts the usage block nested under a `usage`-ish attribute or key, or the
    token counts sitting directly on `result`. Anything it cannot find reads as
    zero rather than raising: a tool that returns no usage should not
    fail-closed every call through a budget policy, it should just contribute
    nothing to the total.
    """
    if result is None:
        return (0, 0)
    usage = _first(result, USAGE_FIELDS)
    if usage is None:
        usage = result  # some tools flatten the counts onto the response
    return (
        int(_first(usage, INPUT_TOKEN_FIELDS) or 0),
        int(_first(usage, OUTPUT_TOKEN_FIELDS) or 0),
    )


def token_count(result: Any) -> int:
    """Total tokens billed for one response."""
    input_tokens, output_tokens = extract_usage(result)
    return input_tokens + output_tokens


def token_cost(
    input_price: float,
    output_price: float,
    *,
    per_tokens: int = 1_000_000,
) -> Callable[[GuardContext], float]:
    """A cost function for `budget_policy(actual_from=...)`.

    `input_price`/`output_price` are per `per_tokens` tokens — a million by
    default, matching how providers publish rates. Pass `per_tokens=1000` for
    per-1k pricing.

        budget_policy(5.0, actual_from=token_cost(3.00, 15.00), tool_name="call_llm")
    """

    def _cost(ctx: GuardContext) -> float:
        input_tokens, output_tokens = extract_usage(ctx.result)
        return (input_tokens * input_price + output_tokens * output_price) / per_tokens

    return _cost


def token_budget_policy(
    max_cost: float,
    *,
    input_price: float,
    output_price: float,
    per_tokens: int = 1_000_000,
    key: str = "llm_cost",
    tool_name: str | None = None,
    name: str | None = None,
    on_fail: Decision = BLOCK,
    severity: Severity = "high",
    escalate_to: str | None = None,
) -> PolicySet:
    """Stop a session once it has spent `max_cost` on model calls.

    Cost is computed from the response's token usage, so the call that crosses
    the threshold completes and the following one is refused — see
    `budget_policy` for why an after-the-fact cap is the only honest shape here.

        token_budget_policy(5.00, input_price=3.00, output_price=15.00,
                            tool_name="call_llm")

    `on_fail=ESCALATE` is often better than a block: a human can decide whether
    this particular run is worth more money.
    """
    return budget_policy(
        max_cost,
        actual_from=token_cost(input_price, output_price, per_tokens=per_tokens),
        key=key,
        tool_name=tool_name,
        name=name or f"token_budget_{max_cost:g}",
        on_fail=on_fail,
        reason=(
            f"session LLM budget of {max_cost:g} is exhausted "
            f"(priced at {input_price:g}/{output_price:g} per {per_tokens:,} tokens)"
        ),
        severity=severity,
        escalate_to=escalate_to,
    )


def token_limit_policy(
    max_tokens: int,
    *,
    key: str = "llm_tokens",
    tool_name: str | None = None,
    name: str | None = None,
    on_fail: Decision = BLOCK,
    severity: Severity = "medium",
    escalate_to: str | None = None,
) -> PolicySet:
    """Cap a session's total token consumption, ignoring price.

    The same shape as `token_budget_policy` but counting tokens, for when the
    limit you actually have is a context or rate quota rather than a dollar
    figure.
    """
    return budget_policy(
        float(max_tokens),
        actual_from=lambda ctx: float(token_count(ctx.result)),
        key=key,
        tool_name=tool_name,
        name=name or f"token_limit_{max_tokens}",
        on_fail=on_fail,
        reason=f"session token limit of {max_tokens:,} is exhausted",
        severity=severity,
        escalate_to=escalate_to,
    )
