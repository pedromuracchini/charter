"""Detect credentials in tool arguments.

An agent that has been prompt-injected, or that simply pasted the wrong
variable, will happily forward an API key to a tool that ships it off-box.
These patterns catch the common, high-confidence shapes — provider keys with
distinctive prefixes, JWTs, PEM private-key blocks.

Deliberately conservative: every pattern here is anchored on a literal marker
(`sk-ant-`, `AKIA`, `-----BEGIN`), so false positives are rare and a BLOCK can
be the default. Detection is not redaction — a matched secret is *not* removed
from `LedgerEvent.args`. See SECURITY.md.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from tollgate.core.context import GuardContext
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK, Decision, Severity

#: (label, pattern) pairs. Anchored on literal prefixes rather than entropy
#: heuristics, which produce far too many false positives on ordinary text.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"\b gh[pousr]_[A-Za-z0-9]{20,}\b".replace(" ", ""))),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
)


def find_secrets(value: object) -> list[str]:
    """Labels of every secret pattern matching anywhere inside `value`.

    Walks dicts, lists and tuples so a credential nested in a JSON payload is
    found, not just a top-level string argument.
    """
    found: list[str] = []
    if isinstance(value, str):
        found.extend(label for label, pattern in SECRET_PATTERNS if pattern.search(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.extend(find_secrets(key))
            found.extend(find_secrets(item))
    elif isinstance(value, list | tuple):
        for item in value:
            found.extend(find_secrets(item))
    return found


def no_secrets_in_args(
    *,
    name: str = "no_secrets_in_args",
    tool_names: Iterable[str] | None = None,
    on_fail: Decision = BLOCK,
    severity: Severity = "high",
    escalate_to: str | None = None,
) -> PolicySet:
    """Reject a call whose arguments contain something shaped like a credential.

    Applies to every tool unless `tool_names` narrows it.

        interceptor = TollgateInterceptor(policies=[no_secrets_in_args()])
    """
    allowed = set(tool_names) if tool_names is not None else None
    policy = PolicySet(
        name,
        active_when=(lambda ctx: ctx.tool_name in allowed) if allowed is not None else None,
    )
    policy.require(
        lambda ctx: not find_secrets(ctx.args),
        on_fail=on_fail,
        reason="tool arguments contain what looks like a credential",
        severity=severity,
        escalate_to=escalate_to,
    )
    return policy


def describe_secrets(ctx: GuardContext) -> str:
    """Human-readable summary of what matched, for a custom `reason`."""
    labels = sorted(set(find_secrets(ctx.args)))
    return ", ".join(labels) if labels else "none"
