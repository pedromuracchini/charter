"""The core evaluation pipeline shared by `@guard` and `TollgateInterceptor`.

Every guarded tool call funnels through `evaluate_call` (or its async sibling
`evaluate_call_async`): build a `GuardContext`, run pre-hook rules (the worst
failure wins, by precedence BLOCK > ESCALATE > ALLOW), resolve any escalation,
execute (or not), run post-hook rules, auto-undo on a post-BLOCK when the call
wraps a `ReversibleAction`, and record a ledger entry plus an OTEL span/metric
for every hook actually evaluated.

Sampling: a failing rule (BLOCK/ESCALATE/log-only ALLOW) is always recorded.
When every applicable rule passes, a single aggregate ALLOW entry is recorded,
sampled at `OtelSettings.allow_sample_rate` (default 1.0 — always).

Modes: only `enforce` may have side effects beyond recording. `dry_run` and
`observe` evaluate every rule and record every decision, but never block, never
undo, and never contact an escalation handler — see `_unresolved_escalation`.

Async: predicates (`pre`/`post`, `active_when`, `applies_to`) are always sync
— only tool invocation (`invoke`), `ReversibleAction.undo_fn`, and
`EscalationHandler.escalate` may be async. `_maybe_await` lets a single
`do_fn`/`undo_fn`/`escalate` be either `def` or `async def` without separate
base classes.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from tollgate._scope import ExecutionScope
from tollgate.core.context import GuardContext
from tollgate.core.escalation import resolve_handler
from tollgate.core.policy_set import Hook, Policy
from tollgate.core.reversible import ReversibleAction
from tollgate.decisions import (
    ALLOW,
    BLOCK,
    ESCALATE,
    GuardBlocked,
    GuardDecision,
    RuleResult,
    pick_decision,
)
from tollgate.ledger.event import ContributingRule, LedgerEvent
from tollgate.ledger.ledger import ActionLedger
from tollgate.multiagent.delegation import delegation_depth
from tollgate.otel.metrics import record_decision, record_delegation_depth, record_escalation
from tollgate.otel.spans import evaluate_span, should_sample
from tollgate.redaction import Redactor, current_redactor

logger = logging.getLogger("tollgate.engine")
_undo_logger = logging.getLogger("tollgate.reversible")
_escalation_logger = logging.getLogger("tollgate.escalation")


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:8]}"


def _is_cross_agent(ctx: GuardContext) -> bool:
    """Heuristic: the call crossed at least one agent-to-agent delegation hop."""
    return ctx.caller_agent_id is not None and len(ctx.delegation_chain) >= 2


def _run_with_timeout(fn: Callable[[], bool | Awaitable[bool]], timeout_s: float) -> bool:
    """Run `fn()` with a hard wall-clock timeout, denying (`False`) if it
    raises or doesn't finish in time — enforces `RuleResult.timeout_s` on
    escalation handlers that don't respect it themselves.

    Uses a disposable single-worker thread pool rather than a `with` block:
    `ThreadPoolExecutor.__exit__` blocks until the submitted task finishes,
    which would defeat the timeout for a genuinely hung `fn`. On timeout, the
    pool is shut down without waiting — the hung thread is abandoned to finish
    (or not) on its own; accepted as a rare-case tradeoff for a misbehaving
    handler, versus blocking the whole tool call indefinitely.

    This is the *sync* path (`evaluate_call`) — an `async def escalate` used
    here (instead of via `evaluate_call_async`/`_run_with_timeout_async`)
    can't be awaited, so it's treated as a caller error and denied rather than
    `bool()`-coercing the unawaited coroutine (which would be truthy and
    silently approve everything).
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    # Copy the caller's context so a handler reading `tollgate.current_scope()`
    # sees the identity that triggered the escalation, not the root scope: a
    # bare worker thread starts with a fresh, empty `contextvars` context.
    future = executor.submit(contextvars.copy_context().run, fn)
    try:
        result = future.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        executor.shutdown(wait=False, cancel_futures=True)
        _escalation_logger.warning(
            "escalation handler exceeded timeout_s=%s — denying (fail-safe)", timeout_s
        )
        return False
    except Exception as exc:
        executor.shutdown(wait=True)
        _escalation_logger.error(
            "escalation handler raised %s: %s — denying (fail-safe)", type(exc).__name__, exc
        )
        return False
    executor.shutdown(wait=True)
    if inspect.isawaitable(result):
        _escalation_logger.error(
            "escalation handler is async but was invoked via the sync engine "
            "(TollgateInterceptor.call() or @guard on a sync tool) — use acall()/an "
            "async tool for an async escalate(); denying (fail-safe)"
        )
        close = getattr(result, "close", None)
        if callable(close):
            close()  # avoid a "coroutine was never awaited" warning for the abandoned result
        return False
    return bool(result)


async def _maybe_await(value: Any) -> Any:
    """`await value` if it's awaitable (a coroutine, mainly), else return it
    as-is — lets `ReversibleAction.do_fn`/`undo_fn` be either `def` or
    `async def` without a separate async-aware class."""
    if inspect.isawaitable(value):
        return await value
    return value


async def _run_with_timeout_async(
    handler_escalate: Callable[[GuardContext, RuleResult], bool | Awaitable[bool]],
    ctx: GuardContext,
    rule_result: RuleResult,
) -> bool:
    """Async sibling of `_run_with_timeout`, for `evaluate_call_async`.

    A plain sync `escalate` would block the whole event loop if awaited
    naively — `asyncio.wait_for`'s timeout can't fire while the loop itself is
    blocked. So a sync `escalate` is dispatched to a thread pool, exactly like
    the sync engine's `_run_with_timeout`; an `async def escalate` is awaited
    directly. Either way, `asyncio.wait_for` enforces `rule_result.timeout_s`.

    The thread pool is a *disposable* one rather than the loop's default
    executor, for the same reason `_run_with_timeout` builds its own: on
    timeout `wait_for` cancels the future but cannot stop the thread already
    running inside it. Parked in the default executor, that abandoned thread
    occupies a shared worker for as long as it runs and makes
    `loop.shutdown_default_executor()` hang at interpreter exit. Here it only
    holds its own pool, which is shut down without waiting and then garbage
    collected once the thread finally returns.

    Context variables are copied into the worker thread, so a handler that
    reads `tollgate.current_scope()` sees the caller's identity rather than the
    root scope.
    """
    try:
        if inspect.iscoroutinefunction(handler_escalate):
            result = await asyncio.wait_for(
                handler_escalate(ctx, rule_result), timeout=rule_result.timeout_s
            )
        else:
            loop = asyncio.get_running_loop()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            context = contextvars.copy_context()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(executor, context.run, handler_escalate, ctx, rule_result),
                    timeout=rule_result.timeout_s,
                )
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
    except TimeoutError:
        _escalation_logger.warning(
            "escalation handler exceeded timeout_s=%s — denying (fail-safe)", rule_result.timeout_s
        )
        return False
    except Exception as exc:
        _escalation_logger.error(
            "escalation handler raised %s: %s — denying (fail-safe)", type(exc).__name__, exc
        )
        return False
    return bool(result)


#: Reason suffix used when an ESCALATE is recorded without contacting a handler
#: because the interceptor is not in `enforce` mode. See `_unresolved_escalation`.
_UNRESOLVED_SUFFIX = " (escalation not resolved — {mode} mode, would block pending approval)"


def _unresolved_escalation(
    rule_result: RuleResult, ctx: GuardContext, mode: str
) -> tuple[GuardDecision, bool]:
    """The decision for an ESCALATE that deliberately was not sent to a handler.

    Outside `enforce` mode the call proceeds no matter what, so contacting the
    handler would buy nothing and cost a real side effect: posting to Slack,
    hitting an approval webhook, or blocking on `input()` for up to `timeout_s`.
    A dry run must be able to answer "what would this policy do?" without
    paging a human. `should_block=True` reports what *would* have happened;
    both engines gate the actual block on `mode == "enforce"` regardless.
    """
    decision = GuardDecision(
        action=ESCALATE,
        reason=rule_result.reason + _UNRESOLVED_SUFFIX.format(mode=mode),
        policy_name=rule_result.policy_name,
        policy_hash=rule_result.policy_hash,
        severity=rule_result.severity,
    )
    record_escalation(
        "not_resolved",
        policy_name=rule_result.policy_name,
        tool_name=ctx.tool_name,
        escalate_to=rule_result.escalate_to,
        latency_ms=0.0,
    )
    return decision, True


def _escalation_decision(
    rule_result: RuleResult, ctx: GuardContext, approved: bool, latency_ms: float
) -> tuple[GuardDecision, bool]:
    """Shared bookkeeping once an escalation handler has answered."""
    suffix = " (escalation approved)" if approved else " (escalation denied/timed out — fail-safe block)"
    record_escalation(
        "approved" if approved else "denied",
        policy_name=rule_result.policy_name,
        tool_name=ctx.tool_name,
        escalate_to=rule_result.escalate_to,
        latency_ms=latency_ms,
    )
    decision = GuardDecision(
        action=ESCALATE,
        reason=rule_result.reason + suffix,
        policy_name=rule_result.policy_name,
        policy_hash=rule_result.policy_hash,
        severity=rule_result.severity,
    )
    return decision, not approved


def _resolve(rule_result: RuleResult, ctx: GuardContext, mode: str) -> tuple[GuardDecision, bool]:
    """Resolve one failing rule to a `GuardDecision` plus whether execution
    should actually be blocked.

    Distinguishes an ESCALATE that was approved (`should_block=False`, the call
    proceeds) from one that was denied or timed out (`should_block=True` —
    fail-safe). The ledger/span `decision` stays `"ESCALATE"` either way; the
    approval outcome is folded into the reason text.

    Escalation handlers are only contacted in `enforce` mode — see
    `_unresolved_escalation`.
    """
    if rule_result.on_fail is ESCALATE:
        if mode != "enforce":
            return _unresolved_escalation(rule_result, ctx, mode)
        handler = resolve_handler(rule_result.escalate_to)
        start = time.perf_counter()
        approved = _run_with_timeout(lambda: handler.escalate(ctx, rule_result), rule_result.timeout_s)
        return _escalation_decision(
            rule_result, ctx, approved, (time.perf_counter() - start) * 1000
        )
    decision = GuardDecision(
        action=rule_result.on_fail,
        reason=rule_result.reason,
        policy_name=rule_result.policy_name,
        policy_hash=rule_result.policy_hash,
        severity=rule_result.severity,
    )
    return decision, rule_result.on_fail is BLOCK


async def _resolve_async(rule_result: RuleResult, ctx: GuardContext, mode: str) -> tuple[GuardDecision, bool]:
    """Async sibling of `_resolve` — identical semantics, but awaits escalation
    via `_run_with_timeout_async` instead of blocking a thread pool."""
    if rule_result.on_fail is ESCALATE:
        if mode != "enforce":
            return _unresolved_escalation(rule_result, ctx, mode)
        handler = resolve_handler(rule_result.escalate_to)
        start = time.perf_counter()
        approved = await _run_with_timeout_async(handler.escalate, ctx, rule_result)
        return _escalation_decision(
            rule_result, ctx, approved, (time.perf_counter() - start) * 1000
        )
    decision = GuardDecision(
        action=rule_result.on_fail,
        reason=rule_result.reason,
        policy_name=rule_result.policy_name,
        policy_hash=rule_result.policy_hash,
        severity=rule_result.severity,
    )
    return decision, rule_result.on_fail is BLOCK


def _record(
    *,
    ctx: GuardContext,
    decision: GuardDecision,
    hook: Hook,
    mode: str,
    undo_op: str | None,
    latency_ms: float,
    tracer_provider: Any = None,
    sampled: bool | None = None,
    ledger: ActionLedger | None = None,
    redactor: Redactor | None = None,
) -> LedgerEvent:
    # A failure (BLOCK/ESCALATE) is always written to the ledger; only its span
    # is sampled, rolled here. An ALLOW's caller has already rolled once for the
    # ledger and passes that same result through, so the two never disagree.
    if sampled is None:
        sampled = should_sample(decision)
    with evaluate_span(
        ctx,
        decision,
        hook,
        dry_run=(mode != "enforce"),
        latency_ms=latency_ms,
        tracer_provider=tracer_provider,
        sampled=sampled,
    ) as span_ids:
        record_decision(decision, ctx.tool_name, latency_ms, _is_cross_agent(ctx))
        # Redaction happens here and nowhere earlier: the policies above have
        # already evaluated against the real `ctx.args`, and this is the last
        # point before the values become durable. `reason`/`undo_op` are
        # scrubbed too — a fail-closed predicate folds its exception text into
        # the reason, and that text routinely quotes the argument that broke it.
        redactor = redactor if redactor is not None else current_redactor()
        event = LedgerEvent(
            event_id=_new_event_id(),
            ts=datetime.now(UTC).isoformat(),
            tool=ctx.tool_name,
            args=redactor.redact_args(ctx.args),
            policy=decision.policy_name,
            decision=decision.action.value.upper(),  # type: ignore[arg-type]
            reason=redactor.redact_text(decision.reason),
            severity=decision.severity,
            hook=hook,
            mode=mode,  # type: ignore[arg-type]
            checksum_expected=ctx.state_checksum,
            checksum_got=ctx.recompute_checksum(),
            undo_op=redactor.redact_text(undo_op) if undo_op else None,
            session_id=ctx.session_id,
            step_index=ctx.step_index,
            caller_agent_id=ctx.caller_agent_id,
            caller_role=ctx.caller_role,
            delegation_chain=list(ctx.delegation_chain),
            trust_level=ctx.trust_level,
            policy_hash=decision.policy_hash,
            contributing_rules=[
                ContributingRule(
                    policy=r.policy_name,
                    reason=redactor.redact_text(r.reason),
                    on_fail=r.on_fail.value.upper(),  # type: ignore[arg-type]
                    severity=r.severity,
                    policy_hash=r.policy_hash,
                )
                for r in decision.rule_results
            ],
            otel_trace_id=span_ids[0] if span_ids else None,
            otel_span_id=span_ids[1] if span_ids else None,
        )
    return (ledger if ledger is not None else ActionLedger.current()).record(event)


def _undo_unavailable(reversible: ReversibleAction, decision: GuardDecision) -> tuple[None, GuardDecision]:
    """Bookkeeping for a post-BLOCK on an action with no `undo_fn`.

    `ReversibleAction.undo()` silently no-ops in that case, and the engine used
    to record `"<name>.undo"` and `undo_executed=True` anyway — a false success
    in the audit trail at exactly the moment nothing was reverted.
    """
    _undo_logger.warning(
        "post-BLOCK on %r, which has no undo_fn — the action already ran and was NOT reverted",
        reversible.name,
    )
    return None, replace(
        decision, reason=f"{decision.reason} (no undo_fn configured — action NOT reverted)"
    )


def _undo_failed(
    reversible: ReversibleAction, decision: GuardDecision, exc: Exception
) -> tuple[str, GuardDecision]:
    """Bookkeeping for an `undo_fn` that raised. Losing the ledger event here
    would defeat the point of having one, so the failure is folded into the
    record rather than propagated."""
    _undo_logger.error(
        "undo for %r failed after a post-BLOCK: %s: %s", reversible.name, type(exc).__name__, exc
    )
    return (
        f"{reversible.name}.undo FAILED: {type(exc).__name__}: {exc}",
        replace(decision, reason=f"{decision.reason} (undo also failed: {type(exc).__name__}: {exc})"),
    )


def _undo_succeeded(reversible: ReversibleAction, decision: GuardDecision) -> tuple[str, GuardDecision]:
    return f"{reversible.name}.undo", replace(decision, undo_executed=True)


def _record_tool_error(
    ctx: GuardContext,
    exc: BaseException,
    *,
    mode: str,
    ledger: ActionLedger | None = None,
    redactor: Redactor | None = None,
) -> None:
    """Record that an authorized tool call raised.

    Without this, a tool that blows up skips every post-hook and writes nothing:
    the call was authorized, ran, and failed, yet the audit trail showed no
    trace of it. Never sampled — tool failures are rare and always interesting.

    No OTEL span is emitted: Tollgate made no decision here, and whatever
    instruments the tool itself owns that part of the trace.
    """
    redactor = redactor if redactor is not None else current_redactor()
    (ledger if ledger is not None else ActionLedger.current()).record(
        LedgerEvent(
            event_id=_new_event_id(),
            ts=datetime.now(UTC).isoformat(),
            tool=ctx.tool_name,
            args=redactor.redact_args(ctx.args),
            policy=None,
            decision="ERROR",
            # An exception message very often echoes the argument that caused
            # it — `KeyError: 'sk-ant-...'` is a real shape.
            reason=redactor.redact_text(f"tool raised {type(exc).__name__}: {exc}"),
            severity="high",
            hook="invoke",
            mode=mode,  # type: ignore[arg-type]
            checksum_expected=ctx.state_checksum,
            checksum_got=ctx.recompute_checksum(),
            session_id=ctx.session_id,
            step_index=ctx.step_index,
            caller_agent_id=ctx.caller_agent_id,
            caller_role=ctx.caller_role,
            delegation_chain=list(ctx.delegation_chain),
            trust_level=ctx.trust_level,
        )
    )


def _aggregate_policy_name(names: list[str]) -> str | None:
    """Collapse the policies that contributed rules to one label: the single
    name if only one did, a `+`-joined label if several did, else `None`."""
    unique = sorted(set(names))
    if not unique:
        return None
    return "+".join(unique)


def _aggregate_policy_hash(contributors: list[tuple[str, str | None]]) -> str | None:
    """The contributing policy's hash, but only when exactly one policy
    contributed — a `+`-joined label has no single fingerprint to report."""
    unique = {name: policy_hash for name, policy_hash in contributors}
    if len(unique) != 1:
        return None
    return next(iter(unique.values()))


def _record_allow(
    ctx: GuardContext,
    *,
    hook: Hook,
    mode: str,
    contributors: list[tuple[str, str | None]],
    tracer_provider: Any = None,
    ledger: ActionLedger | None = None,
    redactor: Redactor | None = None,
) -> None:
    decision = GuardDecision(
        action=ALLOW,
        reason="all applicable rules passed",
        policy_name=_aggregate_policy_name([name for name, _ in contributors]),
        policy_hash=_aggregate_policy_hash(contributors),
    )
    # One roll for both the ledger entry and the span — see `should_sample`.
    if not should_sample(decision):
        return
    _record(
        ctx=ctx,
        decision=decision,
        hook=hook,
        mode=mode,
        undo_op=None,
        latency_ms=0.0,
        tracer_provider=tracer_provider,
        sampled=True,
        ledger=ledger,
        redactor=redactor,
    )


def evaluate_call(
    *,
    tool_name: str,
    args: dict[str, Any],
    invoke: Callable[[], Any],
    policies: Sequence[Policy],
    mode: str,
    scope: ExecutionScope,
    reversible: ReversibleAction | None = None,
    tracer_provider: Any = None,
    ledger: ActionLedger | None = None,
    redactor: Redactor | None = None,
) -> Any:
    """Run one tool call through the full pre/execute/post pipeline.

    Raises `GuardBlocked` in `"enforce"` mode if a pre-hook rule blocks the call
    before it runs, or if a post-hook rule blocks after it already ran (after
    attempting auto-undo, when `reversible` is set). In `"dry_run"`/`"observe"`
    modes the call always proceeds and is never undone — rules are still
    evaluated and every decision is still recorded.

    `tracer_provider`, if given, overrides the globally configured OTEL tracer
    provider for this call's spans (see `TollgateInterceptor.otel_tracer`).
    """
    ctx = GuardContext.build(tool_name=tool_name, args=args, scope=scope)
    if scope.call_state is not None:
        # Before any rule runs, so a rate-limit predicate sees the call it is
        # deciding on. Counts attempts, not successes — see `tollgate.state`.
        scope.call_state.record_call(ctx.session_id, ctx.tool_name)
    record_delegation_depth(delegation_depth(ctx), agent_id=ctx.caller_agent_id)

    pre_results: list[RuleResult] = []
    pre_contributors: list[tuple[str, str | None]] = []
    if reversible is not None:
        intrinsic = reversible.intrinsic_check()
        if intrinsic is not None:
            pre_results.append(intrinsic)
            pre_contributors.append((intrinsic.policy_name, intrinsic.policy_hash))
    for policy in policies:
        policy_results = policy.evaluate(ctx, "pre")
        if policy_results:
            pre_contributors.append((policy.name, policy.policy_hash))
        pre_results.extend(policy_results)

    failed_pre = [r for r in pre_results if not r.passed]
    worst_pre = pick_decision(failed_pre)
    if worst_pre is not None:
        start = time.perf_counter()
        decision, should_block = _resolve(worst_pre, ctx, mode)
        decision = replace(decision, rule_results=tuple(failed_pre))
        latency_ms = (time.perf_counter() - start) * 1000
        _record(
            ctx=ctx,
            decision=decision,
            hook="pre",
            mode=mode,
            undo_op=None,
            latency_ms=latency_ms,
            tracer_provider=tracer_provider,
            ledger=ledger,
            redactor=redactor,
        )
        if should_block and mode == "enforce":
            raise GuardBlocked(decision)
    elif pre_results:
        _record_allow(
            ctx,
            hook="pre",
            mode=mode,
            contributors=pre_contributors,
            tracer_provider=tracer_provider,
            ledger=ledger,
            redactor=redactor,
        )

    snapshot = reversible.snapshot(args) if reversible is not None else None
    try:
        result = invoke()
    except BaseException as exc:
        _record_tool_error(ctx, exc, mode=mode, ledger=ledger, redactor=redactor)
        raise
    ctx.result = result

    post_results: list[RuleResult] = []
    post_contributors: list[tuple[str, str | None]] = []
    for policy in policies:
        policy_results = policy.evaluate(ctx, "post")
        if policy_results:
            post_contributors.append((policy.name, policy.policy_hash))
        post_results.extend(policy_results)

    failed_post = [r for r in post_results if not r.passed]
    worst_post = pick_decision(failed_post)
    if worst_post is not None:
        start = time.perf_counter()
        decision, should_block = _resolve(worst_post, ctx, mode)
        decision = replace(decision, rule_results=tuple(failed_post))
        latency_ms = (time.perf_counter() - start) * 1000
        undo_op = None
        if should_block and mode == "enforce" and reversible is not None:
            if not reversible.is_undoable:
                undo_op, decision = _undo_unavailable(reversible, decision)
            else:
                try:
                    reversible.undo(args, snapshot)
                except Exception as exc:
                    undo_op, decision = _undo_failed(reversible, decision, exc)
                else:
                    undo_op, decision = _undo_succeeded(reversible, decision)
        _record(
            ctx=ctx,
            decision=decision,
            hook="post",
            mode=mode,
            undo_op=undo_op,
            latency_ms=latency_ms,
            tracer_provider=tracer_provider,
            ledger=ledger,
            redactor=redactor,
        )
        if should_block and mode == "enforce":
            raise GuardBlocked(decision)
    elif post_results:
        _record_allow(
            ctx,
            hook="post",
            mode=mode,
            contributors=post_contributors,
            tracer_provider=tracer_provider,
            ledger=ledger,
            redactor=redactor,
        )

    return result


async def evaluate_call_async(
    *,
    tool_name: str,
    args: dict[str, Any],
    invoke: Callable[[], Awaitable[Any]],
    policies: Sequence[Policy],
    mode: str,
    scope: ExecutionScope,
    reversible: ReversibleAction | None = None,
    tracer_provider: Any = None,
    ledger: ActionLedger | None = None,
    redactor: Redactor | None = None,
) -> Any:
    """Async sibling of `evaluate_call` — identical semantics and identical
    ledger/OTEL behavior, for an `async def` tool function.

    `invoke` is awaited instead of called. `ReversibleAction.undo_fn` may be
    sync or async (see `_maybe_await`); `pre_snapshot` stays sync-only.
    Predicates (`pre`/`post`, `active_when`, `applies_to`) are always sync —
    see the module docstring.
    """
    ctx = GuardContext.build(tool_name=tool_name, args=args, scope=scope)
    if scope.call_state is not None:
        # Before any rule runs, so a rate-limit predicate sees the call it is
        # deciding on. Counts attempts, not successes — see `tollgate.state`.
        scope.call_state.record_call(ctx.session_id, ctx.tool_name)
    record_delegation_depth(delegation_depth(ctx), agent_id=ctx.caller_agent_id)

    pre_results: list[RuleResult] = []
    pre_contributors: list[tuple[str, str | None]] = []
    if reversible is not None:
        intrinsic = reversible.intrinsic_check()
        if intrinsic is not None:
            pre_results.append(intrinsic)
            pre_contributors.append((intrinsic.policy_name, intrinsic.policy_hash))
    for policy in policies:
        policy_results = policy.evaluate(ctx, "pre")
        if policy_results:
            pre_contributors.append((policy.name, policy.policy_hash))
        pre_results.extend(policy_results)

    failed_pre = [r for r in pre_results if not r.passed]
    worst_pre = pick_decision(failed_pre)
    if worst_pre is not None:
        start = time.perf_counter()
        decision, should_block = await _resolve_async(worst_pre, ctx, mode)
        decision = replace(decision, rule_results=tuple(failed_pre))
        latency_ms = (time.perf_counter() - start) * 1000
        _record(
            ctx=ctx,
            decision=decision,
            hook="pre",
            mode=mode,
            undo_op=None,
            latency_ms=latency_ms,
            tracer_provider=tracer_provider,
            ledger=ledger,
            redactor=redactor,
        )
        if should_block and mode == "enforce":
            raise GuardBlocked(decision)
    elif pre_results:
        _record_allow(
            ctx,
            hook="pre",
            mode=mode,
            contributors=pre_contributors,
            tracer_provider=tracer_provider,
            ledger=ledger,
            redactor=redactor,
        )

    snapshot = reversible.snapshot(args) if reversible is not None else None
    try:
        result = await invoke()
    except BaseException as exc:
        _record_tool_error(ctx, exc, mode=mode, ledger=ledger, redactor=redactor)
        raise
    ctx.result = result

    post_results: list[RuleResult] = []
    post_contributors: list[tuple[str, str | None]] = []
    for policy in policies:
        policy_results = policy.evaluate(ctx, "post")
        if policy_results:
            post_contributors.append((policy.name, policy.policy_hash))
        post_results.extend(policy_results)

    failed_post = [r for r in post_results if not r.passed]
    worst_post = pick_decision(failed_post)
    if worst_post is not None:
        start = time.perf_counter()
        decision, should_block = await _resolve_async(worst_post, ctx, mode)
        decision = replace(decision, rule_results=tuple(failed_post))
        latency_ms = (time.perf_counter() - start) * 1000
        undo_op = None
        if should_block and mode == "enforce" and reversible is not None:
            if not reversible.is_undoable:
                undo_op, decision = _undo_unavailable(reversible, decision)
            else:
                try:
                    await _maybe_await(reversible.undo(args, snapshot))
                except Exception as exc:
                    undo_op, decision = _undo_failed(reversible, decision, exc)
                else:
                    undo_op, decision = _undo_succeeded(reversible, decision)
        _record(
            ctx=ctx,
            decision=decision,
            hook="post",
            mode=mode,
            undo_op=undo_op,
            latency_ms=latency_ms,
            tracer_provider=tracer_provider,
            ledger=ledger,
            redactor=redactor,
        )
        if should_block and mode == "enforce":
            raise GuardBlocked(decision)
    elif post_results:
        _record_allow(
            ctx,
            hook="post",
            mode=mode,
            contributors=post_contributors,
            tracer_provider=tracer_provider,
            ledger=ledger,
            redactor=redactor,
        )

    return result
