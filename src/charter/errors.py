"""Charter's exception and warning hierarchy.

Every error the framework raises on purpose derives from `CharterError`, so a
caller can catch the library's failures without also catching its own bugs:

    try:
        interceptor.call("delete_record", delete_record, id=1)
    except charter.CharterError:
        ...

Each one also derives from the stdlib exception it used to be (`ValueError`,
`KeyError`, `RuntimeError`, `TypeError`), so an `except ValueError` written
against an earlier version still catches a misconfigured policy. The stdlib
base is part of the contract, not an implementation detail — removing it later
would break callers just as surely as renaming the class.

Warnings are separate. Charter warns rather than raises for configurations
that are legal but almost certainly not what was meant — an escalation target
with no URI scheme denies every call, but so does a deliberate deny-by-default
setup, and only the caller can tell the two apart. `logging` alone was not
enough for these: a `logger.warning` is invisible under the default logging
configuration, which is exactly the audience that most needs to see it.
"""

from __future__ import annotations


class CharterError(Exception):
    """Base class for every error Charter raises deliberately."""


class ConfigurationError(CharterError, ValueError):
    """A policy, action, or handler was built with contradictory options.

    Raised at construction time wherever possible, so a misconfiguration
    surfaces at import rather than as a fail-closed BLOCK in production.
    """


class EscalationError(CharterError, RuntimeError):
    """An escalation channel failed to deliver or resolve an approval request.

    This is the transport failing — a Slack API error, a non-2xx webhook
    response — not a denial. A denied or timed-out escalation is reported as
    `GuardBlocked`, because from the caller's perspective the call was refused.
    The engine catches this and fails closed rather than letting it propagate.
    """


class LedgerError(CharterError):
    """The audit trail could not be read or written."""


class LedgerEventNotFound(LedgerError, KeyError):
    """No ledger event exists with the requested id."""


class AdapterError(CharterError, TypeError):
    """No adapter could wrap the given agent or tool object."""


class CharterWarning(UserWarning):
    """Base class for every warning Charter issues."""


class ConfigurationWarning(CharterWarning):
    """A configuration that is legal, but almost certainly a mistake.

    Warned rather than raised because each case has a legitimate if rare use:
    an escalation target with no scheme (deny-everything by design), a policy
    with no rules (a placeholder being filled in), an interceptor argument
    shadowed by a tool argument of the same name.
    """
