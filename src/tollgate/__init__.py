"""Tollgate — deterministic, code-defined guardrails for AI agent tool calls.

Public API surface. See CLAUDE.md for architecture and usage conventions.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from tollgate._scope import session
from tollgate.core.context import GuardContext
from tollgate.core.decorator import guard
from tollgate.core.escalation import EscalationHandler, register_handler
from tollgate.core.interceptor import TollgateInterceptor
from tollgate.core.policy_set import AndPolicy, NotPolicy, OrPolicy, Policy, PolicySet
from tollgate.core.reversible import ReversibleAction
from tollgate.decisions import (
    ALLOW,
    BLOCK,
    ESCALATE,
    Decision,
    GuardBlocked,
    GuardDecision,
    RuleResult,
)
from tollgate.escalation.cli import CLIEscalationHandler
from tollgate.escalation.slack import SlackEscalationHandler
from tollgate.escalation.webhook import WebhookEscalationHandler
from tollgate.ledger.event import LedgerEvent
from tollgate.ledger.ledger import ActionLedger, ReplayResult, replay
from tollgate.multiagent.delegation import extend_chain, max_delegation_depth_policy
from tollgate.multiagent.registry import AgentIdentity, TollgateRegistry
from tollgate.multiagent.scoped_policy import AgentScopedPolicy
from tollgate.otel.config import configure_otel

try:
    __version__ = version("tollgate")
except PackageNotFoundError:
    # Editable/uninstalled dev checkout (e.g. running from a source tree
    # without `pip install -e .` or `uv sync` having registered it).
    __version__ = "0.0.0+dev"

__all__ = [
    "ALLOW",
    "ActionLedger",
    "AgentIdentity",
    "AgentScopedPolicy",
    "AndPolicy",
    "BLOCK",
    "CLIEscalationHandler",
    "Decision",
    "ESCALATE",
    "EscalationHandler",
    "GuardBlocked",
    "GuardContext",
    "GuardDecision",
    "LedgerEvent",
    "NotPolicy",
    "OrPolicy",
    "Policy",
    "PolicySet",
    "ReplayResult",
    "ReversibleAction",
    "RuleResult",
    "SlackEscalationHandler",
    "TollgateInterceptor",
    "TollgateRegistry",
    "WebhookEscalationHandler",
    "__version__",
    "configure_otel",
    "extend_chain",
    "guard",
    "max_delegation_depth_policy",
    "register_handler",
    "replay",
    "session",
    "wrap",
]


def wrap(agent: Any, interceptor: TollgateInterceptor) -> Any:
    """Wrap `agent`'s tool calls through `interceptor`.

    Equivalent to `interceptor.use(agent)`, exposed at module level so callers
    can write `tollgate.wrap(agent, interceptor)`.
    """
    return interceptor.use(agent)
