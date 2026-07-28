"""Multi-agent identity: the registry, role-scoped policies, delegation."""

from tollgate.multiagent.delegation import (
    delegation_depth,
    extend_chain,
    max_delegation_depth_policy,
)
from tollgate.multiagent.registry import AgentIdentity, TollgateRegistry
from tollgate.multiagent.scoped_policy import AgentScopedPolicy

__all__ = [
    "AgentIdentity",
    "AgentScopedPolicy",
    "TollgateRegistry",
    "delegation_depth",
    "extend_chain",
    "max_delegation_depth_policy",
]
