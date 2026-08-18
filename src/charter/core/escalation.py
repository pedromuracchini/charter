"""Escalation handling: resolving an `ESCALATE` decision to approve or deny.

`EscalationHandler` is pluggable per URI scheme (`escalate_to="slack://..."`,
`"http://..."`, ...). The framework ships only a safe default: when no handler is
registered for a target's scheme, the call is logged and denied — an
unresolved escalation must never silently let the action through.
"""

from __future__ import annotations

import logging
import threading
import warnings
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from urllib.parse import urlsplit

from charter.core.context import GuardContext
from charter.decisions import RuleResult
from charter.errors import ConfigurationError, ConfigurationWarning

logger = logging.getLogger("charter.escalation")


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

    def __repr__(self) -> str:
        return "<FailSafeEscalationHandler denies-everything>"

    def escalate(self, ctx: GuardContext, rule_result: RuleResult) -> bool:
        """Log the request and deny it."""
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
#: Registration usually happens once at startup, but nothing stops an app from
#: swapping a handler while requests are in flight, and `resolve_handler` runs
#: on the hot path for every escalating rule.
_handlers_lock = threading.Lock()


def register_handler(scheme: str, handler: EscalationHandler) -> None:
    """Register a handler for escalation targets of the form `f"{scheme}://..."`.

    Raises:
        ConfigurationError: If `scheme` is empty or itself looks like a URI.
            `resolve_handler` matches on `urlsplit(target).scheme`, so
            registering `"slack://approvals"` as the scheme would never match
            anything.
    """
    if not scheme or "://" in scheme or ":" in scheme:
        raise ConfigurationError(
            f"escalation scheme must be a bare URI scheme like 'slack', not {scheme!r} — "
            f"it is matched against urlsplit(escalate_to).scheme"
        )
    with _handlers_lock:
        _HANDLERS[scheme] = handler


def unregister_handler(scheme: str) -> EscalationHandler | None:
    """Remove the handler registered for `scheme`, returning it if there was
    one. Escalations to that scheme fall back to the fail-safe denier."""
    with _handlers_lock:
        return _HANDLERS.pop(scheme, None)


def registered_handlers() -> dict[str, EscalationHandler]:
    """A snapshot of the scheme → handler registry."""
    with _handlers_lock:
        return dict(_HANDLERS)


def reset_handlers() -> None:
    """Drop every registered handler, restoring the fail-safe default for all
    schemes. Intended for tests — the registry is process-global and otherwise
    leaks between them."""
    with _handlers_lock:
        _HANDLERS.clear()


def validate_escalate_to(escalate_to: str | None, owner: str) -> None:
    """Warn when an escalation target has no URI scheme to resolve.

    `resolve_handler` matches on `urlsplit(target).scheme`, so a plain
    `"security-team"` has an empty scheme, matches no registered handler, and
    falls through to `FailSafeEscalationHandler` — which denies. Every call
    guarded by that rule is then silently blocked forever, with nothing but a
    log line at escalation time to explain why.

    Warned rather than raised: deny-by-default is a legitimate configuration,
    and a handler may legitimately be registered after the policy is built.
    """
    if escalate_to is None or urlsplit(escalate_to).scheme:
        return
    warnings.warn(
        f"{owner}: escalate_to={escalate_to!r} has no URI scheme, so no handler can be "
        f"matched and every escalation will be denied (fail-safe). Use a scheme passed "
        f"to register_handler(), e.g. 'slack://{escalate_to}'.",
        ConfigurationWarning,
        stacklevel=3,
    )


def resolve_handler(escalate_to: str | None) -> EscalationHandler:
    """Find the handler registered for `escalate_to`'s URI scheme, or the
    fail-safe default if none was registered (or `escalate_to` is `None`)."""
    if escalate_to is None:
        return _DEFAULT_HANDLER
    scheme = urlsplit(escalate_to).scheme
    with _handlers_lock:
        return _HANDLERS.get(scheme, _DEFAULT_HANDLER)
