"""Escalation handling: resolving an `ESCALATE` decision to approve or deny.

`EscalationHandler` is pluggable per URI scheme (`escalate_to="slack://..."`,
`"http://..."`, ...). The framework ships only a safe default: when no handler is
registered for a target's scheme, the call is logged and denied — an
unresolved escalation must never silently let the action through.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from urllib.parse import urlsplit

from tollgate.core.context import GuardContext
from tollgate.decisions import RuleResult

logger = logging.getLogger("tollgate.escalation")


class EscalationHandler(ABC):
    """Resolves an ESCALATE decision to an approval/denial.

    Implementations may call out to Slack, a webhook, a human-review queue,
    etc., and may be either `def escalate(...)` or `async def escalate(...)`
    — the engine's `timeout_s` enforcement (see `CLAUDE.md`) applies either
    way, so implementations don't need to manage their own timeout: a sync
    `escalate` that blocks past `timeout_s` is run in a thread pool and denied
    when the deadline passes; an async one is awaited with `asyncio.wait_for`.
    """

    @abstractmethod
    def escalate(self, ctx: GuardContext, rule_result: RuleResult) -> bool | Awaitable[bool]:
        """Return (or resolve to) `True` if approved, `False` if denied."""


class FailSafeEscalationHandler(EscalationHandler):
    """Default handler: no network I/O — logs the escalation and denies it.

    This is the framework's safe default: in the absence of a configured
    integration (Slack, webhook, ...) for the target scheme, an escalation that
    cannot actually be resolved must not be treated as an approval.
    """

    def escalate(self, ctx: GuardContext, rule_result: RuleResult) -> bool:
        logger.warning(
            "escalation %r for tool %r (target=%s, timeout=%ss) has no handler "
            "registered for its target scheme; denying by default (fail-safe)",
            rule_result.reason,
            ctx.tool_name,
            rule_result.escalate_to,
            rule_result.timeout_s,
        )
        return False


_DEFAULT_HANDLER = FailSafeEscalationHandler()
_HANDLERS: dict[str, EscalationHandler] = {}


def register_handler(scheme: str, handler: EscalationHandler) -> None:
    """Register a handler for escalation targets of the form `f"{scheme}://..."`."""
    _HANDLERS[scheme] = handler


def resolve_handler(escalate_to: str | None) -> EscalationHandler:
    """Find the handler registered for `escalate_to`'s URI scheme, or the
    fail-safe default if none was registered (or `escalate_to` is `None`)."""
    if escalate_to is None:
        return _DEFAULT_HANDLER
    scheme = urlsplit(escalate_to).scheme
    return _HANDLERS.get(scheme, _DEFAULT_HANDLER)
