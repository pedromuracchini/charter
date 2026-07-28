"""Restrict which hosts a tool may reach.

Exfiltration is the failure mode: an agent talked into POSTing its context to
an attacker's endpoint. An allowlist is the only reliable answer — a blocklist
loses by construction.

Matching is on the parsed hostname, never a substring of the URL. `"evil.com"`
contains the substring `"example.com"`? No — but
`"https://example.com.evil.com/x"` does, and a substring check would wave it
through. Subdomains match only via an explicit leading dot in the allowlist.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from tollgate.core.context import GuardContext
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK, Decision, Severity


def host_allowed(url: str, domains: Iterable[str]) -> bool:
    """Whether `url`'s host is in `domains`.

    An entry beginning with `.` (`".example.com"`) also matches subdomains;
    a bare entry (`"example.com"`) matches that host exactly. Comparison is
    case-insensitive, and a URL with no parseable host fails closed.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    for entry in domains:
        candidate = entry.lower()
        if candidate.startswith("."):
            if host == candidate[1:] or host.endswith(candidate):
                return True
        elif host == candidate:
            return True
    return False


def domain_allowlist(
    domains: Iterable[str],
    *,
    arg: str = "url",
    tool_names: Iterable[str] | None = None,
    name: str = "domain_allowlist",
    on_fail: Decision = BLOCK,
    severity: Severity = "high",
    escalate_to: str | None = None,
    allowed_schemes: Iterable[str] = ("https",),
) -> PolicySet:
    """Require `ctx.args[arg]` to point at an allowlisted host and scheme.

    `allowed_schemes` defaults to HTTPS only: plain `http://` is both
    interceptable and, via `urllib`, a step away from `file://`. Pass a wider
    tuple deliberately if you need it.

        domain_allowlist([".internal.corp", "api.stripe.com"], tool_names=("http_get",))
    """
    allowed_domains = list(domains)
    schemes = {s.lower() for s in allowed_schemes}
    allowed_tools = set(tool_names) if tool_names is not None else None
    policy = PolicySet(
        name,
        active_when=(lambda ctx: ctx.tool_name in allowed_tools) if allowed_tools is not None else None,
    )

    def _scheme_ok(ctx: GuardContext) -> bool:
        url = ctx.args.get(arg)
        if not isinstance(url, str):
            return False
        return urlsplit(url).scheme.lower() in schemes

    def _host_ok(ctx: GuardContext) -> bool:
        url = ctx.args.get(arg)
        if not isinstance(url, str):
            return False
        return host_allowed(url, allowed_domains)

    policy.require(
        _scheme_ok,
        on_fail=on_fail,
        reason=f"{arg} must use one of these schemes: {', '.join(sorted(schemes))}",
        severity=severity,
        escalate_to=escalate_to,
    )
    policy.require(
        _host_ok,
        on_fail=on_fail,
        reason=f"{arg} points at a host outside the allowlist ({', '.join(allowed_domains)})",
        severity=severity,
        escalate_to=escalate_to,
    )
    return policy
