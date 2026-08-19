"""Multi-agent identity: the registry, role-scoped policies, delegation."""

from chokepoint.multiagent.delegation import (
    delegation_depth,
    extend_chain,
    max_delegation_depth_policy,
)
from chokepoint.multiagent.registry import AgentIdentity, ChokepointRegistry
from chokepoint.multiagent.scoped_policy import AgentScopedPolicy

__all__ = [
    "AgentIdentity",
    "AgentScopedPolicy",
    "ChokepointRegistry",
    "delegation_depth",
    "extend_chain",
    "max_delegation_depth_policy",
]
