"""Multi-agent identity: the registry, role-scoped policies, delegation."""

from charter.multiagent.delegation import (
    delegation_depth,
    extend_chain,
    max_delegation_depth_policy,
)
from charter.multiagent.registry import AgentIdentity, CharterRegistry
from charter.multiagent.scoped_policy import AgentScopedPolicy

__all__ = [
    "AgentIdentity",
    "AgentScopedPolicy",
    "CharterRegistry",
    "delegation_depth",
    "extend_chain",
    "max_delegation_depth_policy",
]
