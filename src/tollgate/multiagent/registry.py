"""`TollgateRegistry` — central registry of agent identities and roles.

Used by `TollgateInterceptor` to populate `ctx.caller_role` / `ctx.trust_level`
for a given `agent_id`, and to attach each agent's own policies. See
"Multi-agent" in CLAUDE.md for when a shared registry is preferable to one
interceptor instance per agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tollgate.core.policy_set import Policy


@dataclass(frozen=True)
class AgentIdentity:
    """One agent's registered identity: its role, policies, and place in the
    delegation graph."""

    agent_id: str
    role: str | None = None
    policies: tuple[Policy, ...] = field(default_factory=tuple)
    delegation_chain: tuple[str, ...] = field(default_factory=tuple)
    trust_level: int = 0


class TollgateRegistry:
    """Maps `agent_id -> AgentIdentity`.

    One registry shared by several `TollgateInterceptor`s is the alternative
    to duplicating role and policy wiring per interceptor — see "Multi-agent"
    in CLAUDE.md for when each shape fits.
    """

    def __init__(self) -> None:
        self._identities: dict[str, AgentIdentity] = {}

    def __repr__(self) -> str:
        agents = ",".join(sorted(self._identities))
        return f"<TollgateRegistry agents={len(self._identities)}[{agents}]>"

    def register(
        self,
        agent_id: str,
        role: str | None = None,
        policies: list[Policy] | tuple[Policy, ...] = (),
        delegation_chain: list[str] | tuple[str, ...] = (),
        trust_level: int = 0,
    ) -> AgentIdentity:
        """Register (or replace) one agent's identity, returning it.

        Args:
            agent_id: The id an interceptor references to adopt this identity.
            role: Matched against `AgentScopedPolicy.allowed_roles`.
            policies: Appended to the policy list of every interceptor
                constructed for this agent.
            delegation_chain: This agent's **ancestors only**, ordered
                outermost first. The acting agent is appended automatically
                when a scope is built, so `ctx.delegation_chain` is the full
                path — see `TollgateInterceptor._self_inclusive_chain`.
            trust_level: Compared by `ctx.trust_at_least(n)`.
        """
        identity = AgentIdentity(
            agent_id=agent_id,
            role=role,
            policies=tuple(policies),
            delegation_chain=tuple(delegation_chain),
            trust_level=trust_level,
        )
        self._identities[agent_id] = identity
        return identity

    def get(self, agent_id: str) -> AgentIdentity | None:
        """The identity registered for `agent_id`, or `None`."""
        return self._identities.get(agent_id)

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._identities

    def all(self) -> list[AgentIdentity]:
        """Every registered identity, in registration order."""
        return list(self._identities.values())
