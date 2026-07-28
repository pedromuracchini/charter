"""Rate-limit and budget policies, built on `tollgate.state.CallState`.

The two history-dependent rules people ask for most: "stop after N calls this
session" and "stop once this session has spent $X". Both need cross-call
memory, which lives in the `CallState` the interceptor injects into the
scope — see that module for why it sits beside `GuardContext` rather than in
it.

Both degrade safely: with no `CallState` attached (a bare `@guard`, or a
`ledger.replay()`) counters read zero, so these policies allow rather than
block on absent history. They are quota enforcement, not a security boundary.
"""

from __future__ import annotations

from collections.abc import Callable

from tollgate.core.context import GuardContext
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK, Decision, Severity
from tollgate.errors import ConfigurationError


def rate_limit_policy(
    max_calls: int,
    *,
    tool_name: str | None = None,
    name: str | None = None,
    on_fail: Decision = BLOCK,
    reason: str | None = None,
    severity: Severity = "medium",
    escalate_to: str | None = None,
) -> PolicySet:
    """Allow at most `max_calls` calls per session.

    Scoped to `tool_name` if given, otherwise counted across every tool the
    session touches. The call being evaluated is already counted, so
    `max_calls=5` permits five and denies the sixth.

    Denied attempts still count — otherwise a caller could retry a denial for
    free and the limit would never bind.

        interceptor = TollgateInterceptor(
            policies=[rate_limit_policy(20, tool_name="send_email")],
        )
    """
    scope_label = tool_name or "any tool"
    policy = PolicySet(
        name or f"rate_limit_{tool_name or 'all'}_{max_calls}",
        active_when=(lambda ctx: ctx.tool_name == tool_name) if tool_name else None,
    )
    counted = tool_name if tool_name is not None else "*"
    policy.require(
        lambda ctx: ctx.calls_this_session(counted) <= max_calls,
        on_fail=on_fail,
        reason=reason or f"rate limit reached: at most {max_calls} calls to {scope_label} per session",
        severity=severity,
        escalate_to=escalate_to,
    )
    return policy


def budget_policy(
    max_total: float,
    amount_from: Callable[[GuardContext], float] | None = None,
    *,
    actual_from: Callable[[GuardContext], float] | None = None,
    key: str = "budget",
    tool_name: str | None = None,
    name: str | None = None,
    on_fail: Decision = BLOCK,
    reason: str | None = None,
    severity: Severity = "high",
    escalate_to: str | None = None,
) -> PolicySet:
    """Cap a session's cumulative spend at `max_total`.

    Two shapes, because a cost is knowable at two very different moments.

    **Knowable up front** — a transfer amount is right there in the arguments.
    Pass `amount_from`, and the pre-hook rejects any call that *would* push the
    running total past `max_total`, so the cap is never exceeded:

        budget_policy(1000.0, lambda ctx: ctx.args["amount"], tool_name="transfer")

    **Only knowable afterwards** — an LLM call's token cost lives in the
    response. Pass `actual_from`, which reads `ctx.result` in the post hook.
    The pre-hook can then only check whether the budget is *already* exhausted,
    so semantics shift from "never exceed" to "stop once spent": the call that
    crosses the line still runs, and the one after it is blocked. That is
    inherent to not knowing the price before you pay it, not a shortcut.

        budget_policy(5.0, actual_from=token_cost(3.0, 15.0), tool_name="call_llm")

    Give both to bound the overshoot: `amount_from` charges an estimate at the
    pre-hook check, `actual_from` supersedes it with the real figure at the
    post hook.

    Spend is recorded by a `hook="post"` rule, so a call blocked in the
    pre-hook is never charged, and neither is one whose tool raised. That rule
    mutates state from inside a predicate, which every other policy in Tollgate
    avoids — the deliberate exception that lets "check then charge" work
    without a third engine hook. It always passes and never blocks anything.

    Either callable raising is handled like any other predicate error: fail
    closed (see `_safety.safe_call`).

    **Concurrency caveat.** The check and the charge are separate hooks, so
    calls running *concurrently within one session* can each pass the check
    before any of them charges, and together overshoot. Budgets are scoped per
    session, and a single agent loop is normally sequential, so this rarely
    bites — but it is a quota, not a hard financial control.
    """
    if amount_from is None and actual_from is None:
        raise ConfigurationError("budget_policy requires amount_from and/or actual_from")

    scope_label = tool_name or "any tool"
    policy = PolicySet(
        name or f"budget_{key}_{max_total:g}",
        active_when=(lambda ctx: ctx.tool_name == tool_name) if tool_name else None,
    )

    if amount_from is not None:
        estimate = amount_from
        policy.require(
            lambda ctx: ctx.spent(key) + float(estimate(ctx)) <= max_total,
            on_fail=on_fail,
            reason=reason or f"session budget of {max_total:g} for {scope_label} would be exceeded",
            severity=severity,
            escalate_to=escalate_to,
        )
    else:
        # No pre-call estimate exists, so the most this can do is refuse to
        # start a call once the budget is already gone.
        policy.require(
            lambda ctx: ctx.spent(key) < max_total,
            on_fail=on_fail,
            reason=reason or f"session budget of {max_total:g} for {scope_label} is exhausted",
            severity=severity,
            escalate_to=escalate_to,
        )

    charge_from = actual_from if actual_from is not None else amount_from
    assert charge_from is not None  # guaranteed by the ValueError above

    def _charge(ctx: GuardContext) -> bool:
        ctx.record_spend(key, float(charge_from(ctx)))
        return True

    policy.require(
        _charge,
        on_fail=on_fail,
        reason=f"recording spend against budget {key!r}",
        hook="post",
        severity="low",
    )
    return policy
