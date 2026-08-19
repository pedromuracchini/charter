"""Chokepoint — deterministic, code-defined guardrails for AI agent tool calls.

Public API surface. See CLAUDE.md for architecture and usage conventions.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from os import PathLike
from typing import Any

from chokepoint import policies
from chokepoint._scope import ExecutionScope, current_scope, session
from chokepoint.core.context import GuardContext
from chokepoint.core.decorator import guard
from chokepoint.core.escalation import (
    EscalationHandler,
    FailSafeEscalationHandler,
    register_handler,
    registered_handlers,
    reset_handlers,
    unregister_handler,
)
from chokepoint.core.interceptor import ChokepointInterceptor, Mode
from chokepoint.core.policy_set import AndPolicy, Hook, NotPolicy, OrPolicy, Policy, PolicySet
from chokepoint.core.reversible import IrreversibilityLevel, ReversibleAction
from chokepoint.decisions import (
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
from chokepoint.errors import (
    AdapterError,
    ChokepointError,
    ChokepointWarning,
    ConfigurationError,
    ConfigurationWarning,
    EscalationError,
    LedgerError,
    LedgerEventNotFound,
)
from chokepoint.escalation.cli import CLIEscalationHandler
from chokepoint.escalation.slack import SlackEscalationHandler
from chokepoint.escalation.webhook import WebhookEscalationHandler
from chokepoint.ledger.event import ContributingRule, LedgerEvent
from chokepoint.ledger.ledger import DEFAULT_MAX_EVENTS, ActionLedger, ReplayResult, replay
from chokepoint.linter.linter import LintFinding, LintSeverity, lint
from chokepoint.multiagent.delegation import (
    delegation_depth,
    extend_chain,
    max_delegation_depth_policy,
)
from chokepoint.multiagent.registry import AgentIdentity, ChokepointRegistry
from chokepoint.multiagent.scoped_policy import AgentScopedPolicy
from chokepoint.otel.config import configure_otel, reset_otel
from chokepoint.redaction import (
    PII_PATTERNS,
    SECRET_PATTERNS,
    NullRedactor,
    PatternRedactor,
    Redactor,
    configure_redaction,
    current_redactor,
    reset_redaction,
)
from chokepoint.report.policy_report import PolicyReport, build_report
from chokepoint.state import ALL_TOOLS, CallState

try:
    __version__ = version("chokepoint")
except PackageNotFoundError:
    # Editable/uninstalled dev checkout (e.g. running from a source tree
    # without `pip install -e .` or `uv sync` having registered it).
    __version__ = "0.0.0+dev"

__all__ = [
    "ALLOW",
    "ALL_TOOLS",
    "BLOCK",
    "ESCALATE",
    "PII_PATTERNS",
    "SECRET_PATTERNS",
    "ActionLedger",
    "AdapterError",
    "AgentIdentity",
    "AgentScopedPolicy",
    "AndPolicy",
    "CLIEscalationHandler",
    "CallState",
    "ChokepointError",
    "ChokepointInterceptor",
    "ChokepointRegistry",
    "ChokepointWarning",
    "ConfigurationError",
    "ConfigurationWarning",
    "ContributingRule",
    "Decision",
    "EscalationError",
    "EscalationHandler",
    "ExecutionScope",
    "FailSafeEscalationHandler",
    "GuardBlocked",
    "GuardContext",
    "GuardDecision",
    "Hook",
    "IrreversibilityLevel",
    "LedgerError",
    "LedgerEvent",
    "LedgerEventNotFound",
    "LintFinding",
    "LintSeverity",
    "Mode",
    "NotPolicy",
    "NullRedactor",
    "OrPolicy",
    "PatternRedactor",
    "Policy",
    "PolicyReport",
    "PolicySet",
    "Redactor",
    "ReplayResult",
    "ReversibleAction",
    "RuleResult",
    "Severity",
    "SlackEscalationHandler",
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
    "policies",
    "register_handler",
    "registered_handlers",
    "replay",
    "reset_handlers",
    "reset_ledger",
    "reset_otel",
    "reset_redaction",
    "session",
    "unregister_handler",
    "wrap",
]


def configure_ledger(
    *,
    sink_path: str | PathLike[str] | None = None,
    max_events: int | None = DEFAULT_MAX_EVENTS,
) -> ActionLedger:
    """Install a freshly configured process-wide `ActionLedger`, and return it.

    This is the only supported way to set `sink_path`, the JSONL file every
    event is mirrored to: the engine writes to `ActionLedger.current()`, which
    builds its lazy default with no arguments, so an `ActionLedger(sink_path=…)`
    you construct yourself is never consulted.

    Args:
        sink_path: JSONL file every event is appended to, regardless of the
            in-memory cap. `None` keeps everything in memory only.
        max_events: In-memory ring-buffer size. `None` for unbounded.

    Returns:
        The newly installed ledger.

    Call once at startup, before the first guarded call — events already
    recorded stay with the old ledger and are **not** migrated. For a
    per-tenant audit trail inside one process, pass `ledger=` to
    `ChokepointInterceptor` instead of configuring the global one.
    """
    return ActionLedger.configure(sink_path=sink_path, max_events=max_events)


def reset_ledger() -> None:
    """Replace the process-wide ledger with a fresh, empty one.

    Drops any `sink_path`/`max_events` previously configured. Intended for
    tests — production code should not need it.
    """
    ActionLedger.reset()


def wrap(agent: Any, interceptor: ChokepointInterceptor) -> Any:
    """Wrap `agent`'s tool calls through `interceptor`.

    Equivalent to `interceptor.use(agent)`, exposed at module level so callers
    can write `chokepoint.wrap(agent, interceptor)`.
    """
    return interceptor.use(agent)
