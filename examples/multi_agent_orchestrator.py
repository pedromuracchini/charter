"""Multi-agent orchestration: one `ChokepointRegistry`, one shared set of
policies, and one `ChokepointInterceptor` per agent — the "shared tool,
identity in context" pattern from CLAUDE.md's "Multi-agent" section
("Pattern 2 — centralized registry", recommended when there are several
agents). The exact same tool functions are reachable by every agent; only the
caller's identity (populated from the registry) decides what's authorized.

Complements the other two multi-agent examples:
- examples/clinical.py — role-based authorization, two agents, one tool.
- examples/delegation_chain.py — delegation-*depth* authorization (how many
  hops an orchestrator→sub-agent→sub-sub-agent call chain crossed).

This one adds: several agents, several tools, role- AND trust-level-based
policies together, and exporting the delegation graph at the end — the
framework's "auto-descriptive" promise applied to a multi-agent system: which
agents can reach which tools, without a separate architecture diagram.

Note on `delegation_chain`: register *ancestors only* — the interceptor
appends the agent's own id when building the scope, so `research_agent` here
registers `("orchestrator",)` and every `GuardContext` sees
`("orchestrator", "research_agent")`. That is the shape the ledger documents
and `export_delegation_graph()` renders edges from.

Run directly:

    uv run python examples/multi_agent_orchestrator.py
"""

from __future__ import annotations

from chokepoint import (
    BLOCK,
    ActionLedger,
    AgentScopedPolicy,
    ChokepointInterceptor,
    ChokepointRegistry,
    GuardBlocked,
)

# 1. One central registry: identity, role, trust level, and delegation
#    lineage for every agent in the system.
registry = ChokepointRegistry()
registry.register("orchestrator", role="orchestrator", trust_level=2)
registry.register("research_agent", role="researcher", trust_level=1, delegation_chain=("orchestrator",))
registry.register("writer_agent", role="writer", trust_level=1, delegation_chain=("orchestrator",))
registry.register("executor_agent", role="executor", trust_level=1, delegation_chain=("orchestrator",))

# 2. Policies shared by every interceptor — role (and, for destructive tools,
#    trust_level) decides which tools an agent may call, regardless of which
#    agent physically holds a reference to the tool function.
search_web_policy = AgentScopedPolicy(
    name="only_researchers_can_search",
    allowed_roles=["researcher", "orchestrator"],
    applies_to=lambda ctx: ctx.tool_name == "search_web",
    on_fail=BLOCK,
    reason="only researchers (or the orchestrator) may search the web",
)
write_file_policy = AgentScopedPolicy(
    name="only_writers_can_write",
    allowed_roles=["writer", "orchestrator"],
    applies_to=lambda ctx: ctx.tool_name == "write_file",
    on_fail=BLOCK,
    reason="only writers (or the orchestrator) may write files",
)
destructive_tools_policy = AgentScopedPolicy(
    name="only_trusted_executors_can_destroy",
    allowed_roles=["executor", "orchestrator"],
    applies_to=lambda ctx: ctx.tool_name in {"delete_file", "execute_code"},
    pre=lambda ctx: ctx.trust_at_least(1),
    on_fail=BLOCK,
    reason="destructive tools require the executor role and trust_level >= 1",
)

POLICIES = [search_web_policy, write_file_policy, destructive_tools_policy]
TOOL_NAMES = ["search_web", "write_file", "delete_file", "execute_code"]


def search_web(query: str) -> dict:
    return {"results": [f"result for {query!r}"]}


def write_file(path: str, content: str) -> dict:
    return {"written": path}


def delete_file(path: str) -> dict:
    return {"deleted": path}


def execute_code(code: str) -> dict:
    return {"output": f"ran: {code}"}


_TOOLS = {
    "search_web": search_web,
    "write_file": write_file,
    "delete_file": delete_file,
    "execute_code": execute_code,
}


def main() -> None:
    ActionLedger.reset()

    interceptors = {
        agent_id: ChokepointInterceptor(registry=registry, agent_id=agent_id, policies=POLICIES)
        for agent_id in ["orchestrator", "research_agent", "writer_agent", "executor_agent"]
    }

    calls = [
        ("orchestrator", "search_web", {"query": "chokepoint"}),
        ("research_agent", "search_web", {"query": "confused deputy prevention"}),
        ("research_agent", "write_file", {"path": "notes.txt", "content": "..."}),
        ("research_agent", "delete_file", {"path": "notes.txt"}),
        ("writer_agent", "write_file", {"path": "report.md", "content": "..."}),
        ("executor_agent", "delete_file", {"path": "tmp.log"}),
        ("executor_agent", "execute_code", {"code": "print('hi')"}),
    ]

    for agent_id, tool_name, args in calls:
        interceptor = interceptors[agent_id]
        try:
            result = interceptor.call(tool_name, _TOOLS[tool_name], **args)
            print(f"{agent_id:>14} -> {tool_name:<12}: allowed -> {result}")
        except GuardBlocked as exc:
            print(f"{agent_id:>14} -> {tool_name:<12}: blocked -> {exc.decision.reason}")

    print("\n=== delegation graph (mermaid) — which agents can reach which tools ===")
    print(ActionLedger.current().export_delegation_graph(format="mermaid"))


if __name__ == "__main__":
    main()
