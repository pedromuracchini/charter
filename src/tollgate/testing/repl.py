"""Minimal policy REPL: evaluate a synthetic `GuardContext` against a set of
policies without running the agent or calling any tool."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tollgate._scope import ExecutionScope
from tollgate.core.context import GuardContext
from tollgate.core.policy_set import Hook, Policy
from tollgate.decisions import RuleResult, pick_decision


@dataclass(frozen=True)
class EvaluationTrace:
    context: GuardContext
    hook: Hook
    results: tuple[RuleResult, ...]
    decision: RuleResult | None


def evaluate_synthetic(
    tool_name: str,
    args: dict[str, Any],
    policies: list[Policy],
    hook: Hook = "pre",
    **scope_kwargs: Any,
) -> EvaluationTrace:
    """Evaluate `policies` against a synthetic context built from `args` and
    `scope_kwargs` (e.g. `caller_role="executor"`) — no tool is ever called."""
    scope = ExecutionScope(**scope_kwargs)
    ctx = GuardContext.build(tool_name=tool_name, args=args, scope=scope)
    results: list[RuleResult] = []
    for policy in policies:
        results.extend(policy.evaluate(ctx, hook))
    failing = [r for r in results if not r.passed]
    return EvaluationTrace(context=ctx, hook=hook, results=tuple(results), decision=pick_decision(failing))


def run_repl(
    policies: list[Policy],
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> None:
    """A minimal interactive loop: prompts for a tool name and JSON args,
    prints the resulting decision and which rules passed/failed. Exits on
    `quit`/EOF.

    `input_fn`/`output_fn` default to the builtins and exist so the loop can be
    driven from a test without a terminal — the same injection point
    `CLIEscalationHandler` uses. Passing neither leaves the behavior identical
    to reading `stdin` and writing `stdout`.
    """
    output_fn("tollgate policy REPL — type a tool name, or 'quit' to exit.")
    while True:
        try:
            tool_name = input_fn("tool> ").strip()
        except EOFError:
            break
        if tool_name in ("quit", "exit", ""):
            break
        try:
            raw_args = input_fn("args (JSON)> ").strip() or "{}"
            args = json.loads(raw_args)
        except (EOFError, json.JSONDecodeError) as exc:
            output_fn(f"invalid input: {exc}")
            continue
        trace = evaluate_synthetic(tool_name, args, policies)
        for result in trace.results:
            mark = "PASS" if result.passed else "FAIL"
            output_fn(f"  [{mark}] {result.policy_name}: {result.reason}")
        if trace.decision is None:
            output_fn("decision: ALLOW (no rule fired)")
        else:
            output_fn(f"decision: {trace.decision.on_fail.value.upper()} — {trace.decision.reason}")
