"""Tollgate — deterministic, code-defined guardrails for AI agent tool calls.

Public API surface. See CLAUDE.md for architecture and usage conventions.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from tollgate._scope import ExecutionScope, current_scope, session
from tollgate.core.context import GuardContext
from tollgate.core.decorator import guard
from tollgate.core.escalation import (
    EscalationHandler,
    FailSafeEscalationHandler,
    register_handler,
    registered_handlers,
    reset_handlers,
    unregister_handler,
)
from tollgate.core.interceptor import Mode, TollgateInterceptor
from tollgate.core.policy_set import AndPolicy, Hook, NotPolicy, OrPolicy, Policy, PolicySet
from tollgate.core.reversible import IrreversibilityLevel, ReversibleAction
from tollgate.decisions import (
    ALLOW,
    BLOCK,
    ESCALATE,
    Decision,
    GuardBlocked,
    GuardDecision,
    RuleResult,
    Severity,
    pick_decision,
)
from tollgate.escalation.cli import CLIEscalationHandler
from tollgate.escalation.slack import SlackEscalationHandler
from tollgate.escalation.webhook import WebhookEscalationHandler
from tollgate.ledger.event import ContributingRule, LedgerEvent
from tollgate.ledger.ledger import ActionLedger, ReplayResult, replay
from tollgate.linter.linter import LintFinding, lint
from tollgate.multiagent.delegation import (
    delegation_depth,
    extend_chain,
    max_delegation_depth_policy,
)
from tollgate.multiagent.registry import AgentIdentity, TollgateRegistry
from tollgate.multiagent.scoped_policy import AgentScopedPolicy
from tollgate.otel.config import configure_otel
from tollgate.redaction import (
    PII_PATTERNS,
    SECRET_PATTERNS,
    NullRedactor,
    PatternRedactor,
    Redactor,
    configure_redaction,
    current_redactor,
)
from tollgate.report.policy_report import PolicyReport, build_report
from tollgate.state import CallState

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
    "CallState",
    "ContributingRule",
    "Decision",
    "ESCALATE",
    "EscalationHandler",
    "ExecutionScope",
    "FailSafeEscalationHandler",
    "GuardBlocked",
    "GuardContext",
    "GuardDecision",
    "Hook",
    "IrreversibilityLevel",
    "LedgerEvent",
    "LintFinding",
    "Mode",
    "NotPolicy",
    "NullRedactor",
    "OrPolicy",
    "PII_PATTERNS",
    "PatternRedactor",
    "Policy",
    "PolicyReport",
    "PolicySet",
    "Redactor",
    "ReplayResult",
    "ReversibleAction",
    "RuleResult",
    "SECRET_PATTERNS",
    "Severity",
    "SlackEscalationHandler",
    "TollgateInterceptor",
    "TollgateRegistry",
    "WebhookEscalationHandler",
    "__version__",
    "build_report",
    "configure_ledger",
    "configure_otel",
    "configure_redaction",
    "current_redactor",
    "current_scope",
    "delegation_depth",
    "extend_chain",
    "guard",
    "lint",
    "max_delegation_depth_policy",
    "pick_decision",
    "register_handler",
    "registered_handlers",
    "replay",
    "reset_handlers",
    "session",
    "unregister_handler",
    "wrap",
]


#: Configure the process-wide `ActionLedger` — most importantly `sink_path`,
#: the JSONL file every event is mirrored to. Call once at startup, before the
#: first guarded call. See `ActionLedger.configure`.
configure_ledger = ActionLedger.configure


def wrap(agent: Any, interceptor: TollgateInterceptor) -> Any:
    """Wrap `agent`'s tool calls through `interceptor`.

    Equivalent to `interceptor.use(agent)`, exposed at module level so callers
    can write `tollgate.wrap(agent, interceptor)`.
    """
    return interceptor.use(agent)
