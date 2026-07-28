"""Core evaluation types: context, policies, decorator, interceptor, escalation.

Re-exported here so `from tollgate.core import PolicySet` works alongside the
deep module paths. Most users should import from the top-level `tollgate`
package; this exists for the names that aren't part of that surface, notably
the `Hook` / `Mode` / `IrreversibilityLevel` type aliases.
"""

from tollgate.core.context import GuardContext
from tollgate.core.decorator import guard
from tollgate.core.escalation import (
    EscalationHandler,
    FailSafeEscalationHandler,
    register_handler,
    registered_handlers,
    reset_handlers,
    resolve_handler,
    unregister_handler,
)
from tollgate.core.interceptor import Mode, TollgateInterceptor
from tollgate.core.policy_set import AndPolicy, Hook, NotPolicy, OrPolicy, Policy, PolicySet
from tollgate.core.reversible import IrreversibilityLevel, ReversibleAction

__all__ = [
    "AndPolicy",
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
    "TollgateInterceptor",
    "guard",
    "register_handler",
    "registered_handlers",
    "reset_handlers",
    "resolve_handler",
    "unregister_handler",
]
