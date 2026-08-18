"""Core evaluation types: context, policies, decorator, interceptor, escalation.

Re-exported here so `from charter.core import PolicySet` works alongside the
deep module paths. Most users should import from the top-level `charter`
package; this exists for the names that aren't part of that surface, notably
the `Hook` / `Mode` / `IrreversibilityLevel` type aliases.
"""

from charter.core.context import GuardContext
from charter.core.decorator import guard
from charter.core.escalation import (
    EscalationHandler,
    FailSafeEscalationHandler,
    register_handler,
    registered_handlers,
    reset_handlers,
    resolve_handler,
    unregister_handler,
)
from charter.core.interceptor import CharterInterceptor, Mode
from charter.core.policy_set import AndPolicy, Hook, NotPolicy, OrPolicy, Policy, PolicySet
from charter.core.reversible import IrreversibilityLevel, ReversibleAction

__all__ = [
    "AndPolicy",
    "CharterInterceptor",
    "EscalationHandler",
    "FailSafeEscalationHandler",
    "GuardContext",
    "Hook",
    "IrreversibilityLevel",
    "Mode",
    "NotPolicy",
    "OrPolicy",
    "Policy",
    "PolicySet",
    "ReversibleAction",
    "guard",
    "register_handler",
    "registered_handlers",
    "reset_handlers",
    "resolve_handler",
    "unregister_handler",
]
