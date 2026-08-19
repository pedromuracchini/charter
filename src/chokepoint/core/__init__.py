"""Core evaluation types: context, policies, decorator, interceptor, escalation.

Re-exported here so `from chokepoint.core import PolicySet` works alongside the
deep module paths. Most users should import from the top-level `chokepoint`
package; this exists for the names that aren't part of that surface, notably
the `Hook` / `Mode` / `IrreversibilityLevel` type aliases.
"""

from chokepoint.core.context import GuardContext
from chokepoint.core.decorator import guard
from chokepoint.core.escalation import (
    EscalationHandler,
    FailSafeEscalationHandler,
    register_handler,
    registered_handlers,
    reset_handlers,
    resolve_handler,
    unregister_handler,
)
from chokepoint.core.interceptor import ChokepointInterceptor, Mode
from chokepoint.core.policy_set import AndPolicy, Hook, NotPolicy, OrPolicy, Policy, PolicySet
from chokepoint.core.reversible import IrreversibilityLevel, ReversibleAction

__all__ = [
    "AndPolicy",
    "ChokepointInterceptor",
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
