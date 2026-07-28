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
    amount_from: Callable[[GuardContext], float],
    *,
    key: str = "budget",
    tool_name: str | None = None,
    name: str | None = None,
    on_fail: Decision = BLOCK,
    reason: str | None = None,
    severity: Severity = "high",
    escalate_to: str | None = None,
) -> PolicySet:
    """Cap a session's cumulative spend at `max_total`.

    `amount_from(ctx)` extracts this call's cost from its arguments — e.g.
    `lambda ctx: ctx.args["amount"]`. The pre-hook rejects a call that *would*
    push the running total past `max_total`, so the cap is never exceeded
    rather than merely detected afterwards.

        budget_policy(1000.0, lambda ctx: ctx.args["amount"], tool_name="transfer")

    The spend is recorded by a `hook="post"` rule, so only calls that actually
    ran are charged. That rule mutates state from inside a predicate, which
    every other policy in Tollgate avoids — it is the deliberate exception that
    lets "check then charge" work without adding a third engine hook. It always
    passes and never blocks anything.

    `amount_from` raising is handled like any other predicate error: fail
    closed (see `_safety.safe_call`).
    """
    scope_label = tool_name or "any tool"
    policy = PolicySet(
        name or f"budget_{key}_{max_total:g}",
        active_when=(lambda ctx: ctx.tool_name == tool_name) if tool_name else None,
    )

    policy.require(
        lambda ctx: ctx.spent(key) + float(amount_from(ctx)) <= max_total,
        on_fail=on_fail,
        reason=reason or f"session budget of {max_total:g} for {scope_label} would be exceeded",
        severity=severity,
        escalate_to=escalate_to,
    )

    def _charge(ctx: GuardContext) -> bool:
        ctx.record_spend(key, float(amount_from(ctx)))
        return True

    policy.require(
        _charge,
        on_fail=on_fail,
        reason=f"recording spend against budget {key!r}",
        hook="post",
        severity="low",
    )
    return policy
