"""Policy linter — best-effort static checks for common authoring mistakes.

Designed to run once after a module's policies/registry are constructed (e.g.
at import time, or via `tollgate lint`), not on every request. It operates on
policy objects and synthetic contexts, never live agent traffic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tollgate._scope import ExecutionScope
from tollgate.core.context import GuardContext
from tollgate.core.policy_set import Policy, PolicySet
from tollgate.multiagent.registry import TollgateRegistry
from tollgate.multiagent.scoped_policy import AgentScopedPolicy

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class LintFinding:
    severity: Severity
    message: str
    policy_name: str | None = None


def lint(
    policies: list[Policy],
    tool_names: list[str] | None = None,
    registry: TollgateRegistry | None = None,
) -> list[LintFinding]:
    findings: list[LintFinding] = []
    findings.extend(_check_dead_policies(policies))
    findings.extend(_check_duplicate_names(policies))
    findings.extend(_check_scoped_policy_without_registry(policies, registry))
    if tool_names is not None:
        findings.extend(_check_uncovered_tools(policies, tool_names))
    return findings


def _check_dead_policies(policies: list[Policy]) -> list[LintFinding]:
    findings = []
    for policy in policies:
        if isinstance(policy, PolicySet) and not policy.rules():
            findings.append(
                LintFinding(
                    severity="warning",
                    message=f"PolicySet '{policy.name}' has no rules registered via require() — never fires",
                    policy_name=policy.name,
                )
            )
    return findings


def _check_duplicate_names(policies: list[Policy]) -> list[LintFinding]:
    counts: dict[str, int] = {}
    for policy in policies:
        counts[policy.name] = counts.get(policy.name, 0) + 1
    return [
        LintFinding(
            severity="warning", message=f"policy name '{name}' is registered {count} times", policy_name=name
        )
        for name, count in counts.items()
        if count > 1
    ]


def _check_scoped_policy_without_registry(
    policies: list[Policy], registry: TollgateRegistry | None
) -> list[LintFinding]:
    has_registered_agents = registry is not None and len(registry.all()) > 0
    findings = []
    for policy in policies:
        is_scoped = isinstance(policy, AgentScopedPolicy) and policy.allowed_roles is not None
        if is_scoped and not has_registered_agents:
            findings.append(
                LintFinding(
                    severity="error",
                    message=(
                        f"AgentScopedPolicy '{policy.name}' restricts allowed_roles, but no agents are "
                        "registered in any TollgateRegistry — caller_role will always be None"
                    ),
                    policy_name=policy.name,
                )
            )
    return findings


def _check_uncovered_tools(policies: list[Policy], tool_names: list[str]) -> list[LintFinding]:
    findings = []
    scope = ExecutionScope()
    for tool_name in tool_names:
        ctx = GuardContext.build(tool_name=tool_name, args={}, scope=scope)
        if not any(policy.is_active(ctx) for policy in policies):
            findings.append(
                LintFinding(
                    severity="warning", message=f"tool '{tool_name}' has no active policy covering it"
                )
            )
    return findings
