"""Policy coverage and delegation graph exporters (DOT / Mermaid / JSON).

Both graphs are built from recorded `LedgerEvent`s, not static policy
introspection — an edge only exists once the corresponding decision has
actually been recorded.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Literal

from tollgate.ledger.event import LedgerEvent

GraphFormat = Literal["dot", "mermaid", "json"]
DelegationFormat = Literal["dot", "mermaid"]

_EDGE_LABEL = {"BLOCK": "block", "ESCALATE": "escalate", "ALLOW": "log"}


def _safe_id(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _policy_edges(events: Iterable[LedgerEvent]) -> list[tuple[str, str, str]]:
    last_decision: dict[tuple[str, str], str] = {}
    for event in events:
        if event.policy is None:
            continue
        last_decision[(event.policy, event.tool)] = event.decision
    return [
        (policy, tool, _EDGE_LABEL.get(decision, decision.lower()))
        for (policy, tool), decision in last_decision.items()
    ]


def policy_graph(events: list[LedgerEvent], format: GraphFormat = "mermaid") -> str:
    """Graph of `policy -> tool` edges colored by the most recently recorded
    decision (block/escalate/log)."""
    edges = _policy_edges(events)
    if format == "json":
        return json.dumps({"edges": [{"policy": p, "tool": t, "action": a} for p, t, a in edges]}, indent=2)
    if format == "mermaid":
        lines = ["graph LR"]
        for policy, tool, action in edges:
            lines.append(f'  {_safe_id(policy)}["{policy}"] -->|{action}| {_safe_id(tool)}["{tool}"]')
        return "\n".join(lines)
    if format == "dot":
        lines = ["digraph policies {"]
        for policy, tool, action in edges:
            lines.append(f'  "{policy}" -> "{tool}" [label="{action}"];')
        lines.append("}")
        return "\n".join(lines)
    raise ValueError(f"unsupported graph format: {format!r}")


def _delegation_edges(
    events: Iterable[LedgerEvent],
) -> tuple[list[tuple[str, str, int]], list[tuple[str, str, str]]]:
    agent_edges: dict[tuple[str, str], int] = {}
    tool_edges: dict[tuple[str, str], str] = {}
    for event in events:
        chain = event.delegation_chain
        for caller, callee in zip(chain, chain[1:], strict=False):
            agent_edges[(caller, callee)] = event.trust_level
        if event.caller_agent_id:
            label = _EDGE_LABEL.get(event.decision, event.decision.lower())
            tool_edges[(event.caller_agent_id, event.tool)] = label
    return (
        [(a, b, trust) for (a, b), trust in agent_edges.items()],
        [(a, t, action) for (a, t), action in tool_edges.items()],
    )


def delegation_graph(events: list[LedgerEvent], format: DelegationFormat = "mermaid") -> str:
    """Graph of which agents can call which tools, and the delegation hops
    between agents, with the trust level recorded on each agent-to-agent edge."""
    agent_edges, tool_edges = _delegation_edges(events)
    if format == "mermaid":
        lines = ["graph LR"]
        for a, b, trust in agent_edges:
            lines.append(f"  {_safe_id(a)} -->|trust={trust}| {_safe_id(b)}")
        for a, t, action in tool_edges:
            lines.append(f"  {_safe_id(a)} -->|{action}| {_safe_id(t)}")
        return "\n".join(lines)
    if format == "dot":
        lines = ["digraph delegation {"]
        for a, b, trust in agent_edges:
            lines.append(f'  "{a}" -> "{b}" [label="trust={trust}"];')
        for a, t, action in tool_edges:
            lines.append(f'  "{a}" -> "{t}" [label="{action}"];')
        lines.append("}")
        return "\n".join(lines)
    raise ValueError(f"unsupported graph format: {format!r}")
