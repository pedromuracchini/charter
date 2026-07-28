"""Helpers for working with `delegation_chain` and `trust_level`."""

from __future__ import annotations

from tollgate.core.context import GuardContext
from tollgate.core.policy_set import Hook
from tollgate.decisions import BLOCK, Decision
from tollgate.multiagent.scoped_policy import AgentScopedPolicy


def delegation_depth(ctx: GuardContext) -> int:
    """Number of hops in `ctx.delegation_chain`."""
    return len(ctx.delegation_chain)


def extend_chain(chain: tuple[str, ...], agent_id: str) -> tuple[str, ...]:
    """Append `agent_id` to a delegation chain — e.g. before an orchestrator
    delegates a call to another agent."""
    return (*chain, agent_id)


def max_delegation_depth_policy(
    max_depth: int,
    *,
    name: str = "max_delegation_depth",
    on_fail: Decision = BLOCK,
    reason: str | None = None,
    hook: Hook = "pre",
) -> AgentScopedPolicy:
    """Convenience constructor for the "reject calls that crossed too many
    agent hops" rule (the `depth_policy` pattern from CLAUDE.md)."""
    return AgentScopedPolicy(
        name=name,
        on_fail=on_fail,
        reason=reason or f"delegation chain exceeds max depth of {max_depth}",
        pre=lambda ctx: delegation_depth(ctx) <= max_depth,
        hook=hook,
    )
