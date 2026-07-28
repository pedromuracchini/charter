"""`AgentScopedPolicy` — a policy that only fires for certain caller roles.

The main building block for multi-agent authorization: combine `allowed_roles`
(who may call) with `applies_to` (which tool calls this restricts) to express
rules like "only executor/admin may call `delete_*` tools". `pre`/`hook` allow
arbitrary additional predicates (e.g. delegation-depth limits) beyond role
checks.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Iterable

from tollgate._safety import safe_call
from tollgate.core.context import GuardContext
from tollgate.core.policy_set import Hook, Policy
from tollgate.decisions import BLOCK, Decision, RuleResult, Severity
from tollgate.errors import ConfigurationError

logger = logging.getLogger("tollgate.policy")


class AgentScopedPolicy(Policy):
    def __init__(
        self,
        name: str,
        on_fail: Decision,
        reason: str,
        allowed_roles: Iterable[str] | None = None,
        applies_to: Callable[[GuardContext], bool] | None = None,
        pre: Callable[[GuardContext], bool] | None = None,
        escalate_to: str | None = None,
        timeout_s: int = 300,
        severity: Severity = "medium",
        hook: Hook = "pre",
    ) -> None:
        if allowed_roles is None and pre is None:
            raise ConfigurationError("AgentScopedPolicy requires allowed_roles and/or pre")
        self.name = name
        self.on_fail = on_fail
        self.reason = reason
        self.allowed_roles = tuple(allowed_roles) if allowed_roles is not None else None
        self._applies_to = applies_to or (lambda ctx: True)
        self._pre = pre
        self.escalate_to = escalate_to
        self.timeout_s = timeout_s
        self.severity = severity
        self.hook: Hook = hook

    def is_active(self, ctx: GuardContext) -> bool:
        """Whether `applies_to` applies. Fails safe: if `applies_to` itself
        raises, the policy is treated as active (never silently skipped)."""
        try:
            return bool(self._applies_to(ctx))
        except Exception as exc:
            logger.warning(
                "AgentScopedPolicy %r applies_to raised %s: %s — treating as active (fail-safe)",
                self.name,
                type(exc).__name__,
                exc,
            )
            return True

    def _predicate(self, ctx: GuardContext) -> bool:
        if self.allowed_roles is not None and ctx.caller_role not in self.allowed_roles:
            return False
        return not (self._pre is not None and not self._pre(ctx))

    def evaluate(self, ctx: GuardContext, hook: Hook) -> list[RuleResult]:
        if hook != self.hook or not self.is_active(ctx):
            return []
        passed, error = safe_call(self._predicate, ctx)
        if error is not None:
            # A broken predicate can't be trusted to honor its own on_fail/
            # severity — always fail closed, regardless of what was declared.
            return [
                RuleResult(
                    passed=False,
                    on_fail=BLOCK,
                    reason=f"{self.reason} — predicate raised {error}",
                    policy_name=self.name,
                    severity="high",
                    timeout_s=self.timeout_s,
                    policy_hash=self.policy_hash,
                )
            ]
        return [
            RuleResult(
                passed=passed,
                on_fail=self.on_fail,
                reason=self.reason,
                policy_name=self.name,
                severity=self.severity,
                escalate_to=self.escalate_to,
                timeout_s=self.timeout_s,
                policy_hash=self.policy_hash,
            )
        ]

    @property
    def policy_hash(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.name.encode())
        digest.update(repr(self.allowed_roles).encode())
        digest.update(self.reason.encode())
        digest.update(self.on_fail.value.encode())
        return f"sha256:{digest.hexdigest()[:16]}"
