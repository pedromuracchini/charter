"""`Policy` interface, `PolicySet`, and logical composition (`&`, `|`, `~`).

A `Policy` is anything that can decide, for a given `GuardContext` and hook
("pre" or "post"), whether it applies (`is_active`) and which rules pass or fail
(`evaluate`). `PolicySet` is the concrete, user-facing implementation: a named,
optionally domain-scoped group of rules that are ANDed together. `AndPolicy`,
`OrPolicy`, and `NotPolicy` compose any `Policy` — including other composites —
so a coverage graph or report can reflect the real logical structure.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Literal

from charter._safety import safe_call
from charter.core.context import GuardContext
from charter.core.escalation import validate_escalate_to
from charter.decisions import BLOCK, Decision, RuleResult, Severity

Hook = Literal["pre", "post"]

logger = logging.getLogger("charter.policy")


class Policy(ABC):
    """Base interface implemented by `PolicySet`, `AgentScopedPolicy`, and the
    composite policies built by `&`, `|`, and `~`."""

    name: str

    @abstractmethod
    def is_active(self, ctx: GuardContext) -> bool:
        """Whether this policy applies at all to `ctx` (independent of hook)."""

    @abstractmethod
    def evaluate(self, ctx: GuardContext, hook: Hook) -> list[RuleResult]:
        """Evaluate every rule registered for `hook`. An empty list means either
        the policy is inactive or every applicable rule passed."""

    @property
    @abstractmethod
    def policy_hash(self) -> str:
        """Deterministic hash of this policy's rules, for versioning and audit."""

    def __and__(self, other: Policy) -> AndPolicy:
        return AndPolicy(self, other)

    def __or__(self, other: Policy) -> OrPolicy:
        return OrPolicy(self, other)

    def __invert__(self) -> NotPolicy:
        return NotPolicy(self)


class _Rule:
    """A single predicate registered on a `PolicySet` via `require()`."""

    __slots__ = (
        "escalate_to",
        "hook",
        "on_fail",
        "predicate",
        "reason",
        "severity",
        "timeout_s",
    )

    def __init__(
        self,
        predicate: Callable[[GuardContext], bool],
        on_fail: Decision,
        reason: str,
        escalate_to: str | None,
        timeout_s: int,
        severity: Severity,
        hook: Hook,
    ) -> None:
        self.predicate = predicate
        self.on_fail = on_fail
        self.reason = reason
        self.escalate_to = escalate_to
        self.timeout_s = timeout_s
        self.severity = severity
        self.hook = hook

    def source(self) -> str:
        """Best-effort stable text for hashing; falls back to the qualified name
        when source isn't available (e.g. predicate defined in a REPL)."""
        try:
            return inspect.getsource(self.predicate).strip()
        except (OSError, TypeError):
            return getattr(self.predicate, "__qualname__", repr(self.predicate))


class PolicySet(Policy):
    """A named, optionally domain-scoped group of rules, ANDed together.

    `active_when` gates the whole set (e.g. only for `ctx.domain == "healthcare"`).
    Each rule added via `require()` is evaluated independently; the engine treats
    any failing rule as a failure of the set (first-failure-wins, by decision
    precedence BLOCK > ESCALATE > ALLOW).
    """

    def __init__(
        self,
        name: str,
        active_when: Callable[[GuardContext], bool] | None = None,
    ) -> None:
        self.name = name
        self._active_when = active_when or (lambda ctx: True)
        self._rules: list[_Rule] = []
        # Memoized policy_hash. Every evaluate() stamps the hash onto each
        # RuleResult, so recomputing a SHA-256 over inspect.getsource() of every
        # predicate on each tool call would be real hot-path cost. Invalidated
        # by require(), the only thing that changes the inputs.
        self._hash_cache: str | None = None

    def __repr__(self) -> str:
        pre = sum(1 for rule in self._rules if rule.hook == "pre")
        post = len(self._rules) - pre
        return f"<PolicySet {self.name!r} pre={pre} post={post} hash={self.policy_hash}>"

    def require(
        self,
        predicate: Callable[[GuardContext], bool],
        *,
        on_fail: Decision,
        reason: str,
        escalate_to: str | None = None,
        timeout_s: int = 300,
        severity: Severity = "medium",
        hook: Hook = "pre",
    ) -> PolicySet:
        """Register a rule. Returns `self` so calls can be chained if desired.

        Args:
            predicate: Returns `True` when the call is acceptable. Evaluated
                against a real `GuardContext`; an exception fails closed.
            on_fail: `BLOCK`, `ESCALATE`, or `ALLOW` (log-only — recorded but
                never enforced).
            reason: Recorded on the ledger event and carried in `GuardBlocked`.
            escalate_to: URI whose scheme selects a registered
                `EscalationHandler`. Only meaningful with `on_fail=ESCALATE`.
            timeout_s: How long an escalation may take before being denied.
            severity: Recorded on the event; breaks ties between rules failing
                together at the same decision level.
            hook: Run before (`"pre"`) or after (`"post"`) the tool executes.
        """
        validate_escalate_to(escalate_to, f"PolicySet {self.name!r}")
        self._rules.append(_Rule(predicate, on_fail, reason, escalate_to, timeout_s, severity, hook))
        self._hash_cache = None
        return self

    def is_active(self, ctx: GuardContext) -> bool:
        """Whether `active_when` applies. Fails safe: if `active_when` itself
        raises, the policy is treated as active (never silently skipped)."""
        try:
            return bool(self._active_when(ctx))
        except Exception as exc:
            logger.warning(
                "PolicySet %r active_when raised %s: %s — treating as active (fail-safe)",
                self.name,
                type(exc).__name__,
                exc,
            )
            return True

    def evaluate(self, ctx: GuardContext, hook: Hook) -> list[RuleResult]:
        if not self.is_active(ctx):
            return []
        results = []
        policy_hash = self.policy_hash
        for rule in self._rules:
            if rule.hook != hook:
                continue
            passed, error = safe_call(rule.predicate, ctx)
            if error is not None:
                # A broken predicate can't be trusted to honor its own on_fail/
                # severity — always fail closed, regardless of what was declared.
                results.append(
                    RuleResult(
                        passed=False,
                        on_fail=BLOCK,
                        reason=f"{rule.reason} — predicate raised {error}",
                        policy_name=self.name,
                        severity="high",
                        timeout_s=rule.timeout_s,
                        policy_hash=policy_hash,
                    )
                )
                continue
            results.append(
                RuleResult(
                    passed=passed,
                    on_fail=rule.on_fail,
                    reason=rule.reason,
                    policy_name=self.name,
                    severity=rule.severity,
                    escalate_to=rule.escalate_to,
                    timeout_s=rule.timeout_s,
                    policy_hash=policy_hash,
                )
            )
        return results

    def rules(self, hook: Hook | None = None) -> list[_Rule]:
        """Read-only access to registered rules, for the linter and reports."""
        if hook is None:
            return list(self._rules)
        return [r for r in self._rules if r.hook == hook]

    @property
    def policy_hash(self) -> str:
        if self._hash_cache is not None:
            return self._hash_cache
        digest = hashlib.sha256()
        digest.update(self.name.encode())
        for rule in self._rules:
            digest.update(rule.hook.encode())
            digest.update(rule.source().encode())
            digest.update(rule.reason.encode())
            digest.update(rule.on_fail.value.encode())
        self._hash_cache = f"sha256:{digest.hexdigest()[:16]}"
        return self._hash_cache


class _CompositePolicy(Policy):
    """Shared plumbing for `AndPolicy` / `OrPolicy` / `NotPolicy`."""

    def __init__(self, *children: Policy, name: str) -> None:
        self._children = children
        self.name = name
        self._hash_cache: tuple[tuple[str, ...], str] | None = None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r} hash={self.policy_hash}>"

    @property
    def policy_hash(self) -> str:
        # Keyed on the children's own hashes rather than memoized outright: a
        # child `PolicySet` can still gain rules through `require()` after the
        # composite was built, and it has no way to notify its parents. Reading
        # the children is cheap (they memoize), so this only skips the SHA-256.
        child_hashes = tuple(child.policy_hash for child in self._children)
        if self._hash_cache is not None and self._hash_cache[0] == child_hashes:
            return self._hash_cache[1]
        digest = hashlib.sha256()
        digest.update(self.name.encode())
        for child_hash in child_hashes:
            digest.update(child_hash.encode())
        composite_hash = f"sha256:{digest.hexdigest()[:16]}"
        self._hash_cache = (child_hashes, composite_hash)
        return composite_hash


class AndPolicy(_CompositePolicy):
    """`policy_a & policy_b` — active when either child is active; failing any
    rule from any active child fails the AND (constraints simply accumulate)."""

    def __init__(self, left: Policy, right: Policy) -> None:
        super().__init__(left, right, name=f"({left.name} & {right.name})")

    def is_active(self, ctx: GuardContext) -> bool:
        return self._children[0].is_active(ctx) or self._children[1].is_active(ctx)

    def evaluate(self, ctx: GuardContext, hook: Hook) -> list[RuleResult]:
        results: list[RuleResult] = []
        for child in self._children:
            results.extend(child.evaluate(ctx, hook))
        return results


class OrPolicy(_CompositePolicy):
    """`policy_a | policy_b` — passes if at least one active child fully passes
    for this hook; if every active child has a failure, all of their failures
    are reported together."""

    def __init__(self, left: Policy, right: Policy) -> None:
        super().__init__(left, right, name=f"({left.name} | {right.name})")

    def is_active(self, ctx: GuardContext) -> bool:
        return self._children[0].is_active(ctx) or self._children[1].is_active(ctx)

    def evaluate(self, ctx: GuardContext, hook: Hook) -> list[RuleResult]:
        active_children = [c for c in self._children if c.is_active(ctx)]
        if not active_children:
            return []
        per_child = [c.evaluate(ctx, hook) for c in active_children]
        if any(all(r.passed for r in results) for results in per_child):
            return []
        return [r for results in per_child for r in results if not r.passed]


class NotPolicy(_CompositePolicy):
    """`~policy_a` — inverts the child as a whole: fails (BLOCKs, by default)
    when the child *was applicable* (active, with rules registered for this
    hook) but raised no violation, and passes when the child has at least one
    failing rule. If the child isn't applicable to this hook/context at all
    (inactive, or simply has no rules for this hook — its `evaluate()` then
    returns `[]` the same way `PolicySet.evaluate()` does for "not applicable"),
    `NotPolicy` is not applicable either: `[]`, not a synthesized block. A
    `PolicySet`'s `evaluate()` always includes an entry per matching rule
    regardless of pass/fail, so an empty result unambiguously means
    "not applicable" — never "applicable and all rules passed"."""

    def __init__(self, child: Policy) -> None:
        super().__init__(child, name=f"~{child.name}")
        self._child = child

    def is_active(self, ctx: GuardContext) -> bool:
        return self._child.is_active(ctx)

    def evaluate(self, ctx: GuardContext, hook: Hook) -> list[RuleResult]:
        child_results = self._child.evaluate(ctx, hook)
        if not child_results:
            return []
        if any(not r.passed for r in child_results):
            return []
        template = child_results[0]
        return [
            RuleResult(
                passed=False,
                on_fail=template.on_fail,
                reason=f"negated policy '{self._child.name}' raised no violation",
                policy_name=self.name,
                severity=template.severity,
            )
        ]
