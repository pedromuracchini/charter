"""Natural-language narrative export — a plain-English summary of ledger
activity for non-technical stakeholders (compliance, management)."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from tollgate.ledger.event import LedgerEvent

Audience = Literal["technical", "non-technical"]


def narrative(events: list[LedgerEvent], audience: Audience = "non-technical") -> str:
    if not events:
        return "No tool calls have been recorded yet."

    by_decision = Counter(e.decision for e in events)
    by_policy = Counter(e.policy for e in events if e.policy)
    tools = sorted({e.tool for e in events})

    if audience == "non-technical":
        tool_list = ", ".join(tools)
        sentences = [f"Tollgate recorded {len(events)} decisions across {len(tools)} tools ({tool_list})."]
        if by_decision.get("BLOCK"):
            sentences.append(f"{by_decision['BLOCK']} action(s) were blocked.")
        if by_decision.get("ESCALATE"):
            sentences.append(f"{by_decision['ESCALATE']} action(s) required escalation or approval.")
        if by_decision.get("ALLOW"):
            sentences.append(f"{by_decision['ALLOW']} action(s) were allowed.")
        if by_policy:
            top_policy, top_count = by_policy.most_common(1)[0]
            sentences.append(f"Most active policy: '{top_policy}', involved in {top_count} decision(s).")
        return " ".join(sentences)

    lines = [f"{len(events)} events across {len(tools)} tools: {', '.join(tools)}."]
    lines.append(f"decisions: {dict(by_decision)}")
    for policy, count in by_policy.most_common():
        lines.append(f"  - {policy}: {count} decision(s)")
    return "\n".join(lines)
