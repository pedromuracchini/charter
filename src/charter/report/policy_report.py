"""Policy inventory and coverage report.

Built from a set of registered policies plus the ledger activity that shows
which tools they actually fired for. Coverage combines two signals by default:
- *dynamic*: a tool that has at least one policy-attributed decision recorded
  in `events`.
- *static*: a tool that some policy's `is_active()` would apply to right now,
  via a synthetic context — the same check `charter.linter` uses — so a
  policy that hasn't fired yet still shows up as covered.

Pass `include_static=False` for the purely ledger-driven (audit) view: only
tools that have actually been decided on count as covered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from charter._scope import ExecutionScope
from charter.core.context import GuardContext
from charter.core.policy_set import Policy
from charter.ledger.event import LedgerEvent

DEFAULT_WINDOW_HOURS = 24.0


@dataclass(frozen=True)
class PolicyStats:
    name: str
    policy_hash: str
    block_count: int
    escalate_count: int
    allow_count: int
    tools_covered: tuple[str, ...]


@dataclass(frozen=True)
class PolicyReport:
    policies: tuple[PolicyStats, ...]
    window_hours: float
    coverage_ratio: float
    covered_tools: tuple[str, ...]
    uncovered_tools: tuple[str, ...]


def _within_window(event: LedgerEvent, now: datetime, window_hours: float) -> bool:
    try:
        ts = datetime.fromisoformat(event.ts)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return now - ts <= timedelta(hours=window_hours)


def _statically_covered_tools(policies: list[Policy], tool_names: set[str]) -> set[str]:
    scope = ExecutionScope()
    covered = set()
    for tool_name in tool_names:
        ctx = GuardContext.build(tool_name=tool_name, args={}, scope=scope)
        if any(policy.is_active(ctx) for policy in policies):
            covered.add(tool_name)
    return covered


def build_report(
    policies: list[Policy],
    events: list[LedgerEvent],
    all_tool_names: list[str] | None = None,
    now: datetime | None = None,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    include_static: bool = True,
) -> PolicyReport:
    """Build a `PolicyReport` from registered `policies` and recorded `events`.

    `all_tool_names`, if given, is the universe of tools for computing
    coverage; otherwise it's inferred from every tool name seen in `events`.
    `window_hours` controls the recency window for each policy's
    block/escalate/allow counts (default 24h). `include_static` (default
    `True`) additionally counts a tool as covered if some policy's
    `is_active()` applies to it, even with zero recorded activity.
    """
    now = now or datetime.now(UTC)
    recent_by_policy: dict[str, list[LedgerEvent]] = {}
    tools_by_policy: dict[str, set[str]] = {}
    dynamically_covered: set[str] = set()

    for event in events:
        if event.policy is None:
            continue
        dynamically_covered.add(event.tool)
        tools_by_policy.setdefault(event.policy, set()).add(event.tool)
        if _within_window(event, now, window_hours):
            recent_by_policy.setdefault(event.policy, []).append(event)

    stats = []
    for policy in policies:
        recent = recent_by_policy.get(policy.name, [])
        stats.append(
            PolicyStats(
                name=policy.name,
                policy_hash=policy.policy_hash,
                block_count=sum(1 for e in recent if e.decision == "BLOCK"),
                escalate_count=sum(1 for e in recent if e.decision == "ESCALATE"),
                allow_count=sum(1 for e in recent if e.decision == "ALLOW"),
                tools_covered=tuple(sorted(tools_by_policy.get(policy.name, ()))),
            )
        )

    universe = set(all_tool_names) if all_tool_names is not None else {e.tool for e in events}
    covered = {t for t in universe if t in dynamically_covered}
    if include_static:
        covered |= _statically_covered_tools(policies, universe)
    uncovered = universe - covered
    ratio = (len(covered) / len(universe)) if universe else 1.0

    return PolicyReport(
        policies=tuple(stats),
        window_hours=window_hours,
        coverage_ratio=ratio,
        covered_tools=tuple(sorted(covered)),
        uncovered_tools=tuple(sorted(uncovered)),
    )
