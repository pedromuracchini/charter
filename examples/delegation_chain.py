"""Delegation-depth confused-deputy prevention: block a tool call if it
crossed too many agent-to-agent delegation hops, using
`max_delegation_depth_policy` — complements examples/clinical.py's
role-based scenario with a chain-depth-based one.

Run directly:

    uv run python examples/delegation_chain.py
"""

from __future__ import annotations

from charter import CharterInterceptor, CharterRegistry, GuardBlocked, max_delegation_depth_policy

registry = CharterRegistry()
registry.register("orchestrator", role="orchestrator", trust_level=2)
registry.register("research_agent", role="researcher", trust_level=1, delegation_chain=("orchestrator",))
registry.register(
    "sub_agent",
    role="researcher",
    trust_level=1,
    delegation_chain=("orchestrator", "research_agent"),
)
registry.register(
    "sub_sub_agent",
    role="researcher",
    trust_level=0,
    delegation_chain=("orchestrator", "research_agent", "sub_agent"),
)

# Reject calls that crossed more than 2 agent-to-agent delegation hops.
depth_policy = max_delegation_depth_policy(2)


def search_web(query: str) -> dict:
    return {"results": [f"result for {query!r}"]}


def main() -> None:
    for agent_id in ["research_agent", "sub_agent", "sub_sub_agent"]:
        interceptor = CharterInterceptor(registry=registry, agent_id=agent_id, policies=[depth_policy])
        try:
            result = interceptor.call("search_web", search_web, query="charter")
            print(f"{agent_id}: allowed -> {result}")
        except GuardBlocked as exc:
            print(f"{agent_id}: blocked -> {exc.decision.reason}")


if __name__ == "__main__":
    main()
