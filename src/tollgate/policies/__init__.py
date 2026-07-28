"""Ready-made policies for the checks nearly every agent needs.

Every policy elsewhere in Tollgate is something you write: `PolicySet(...)` plus
a lambda. That is the right primitive, but it means each new project starts
from a blank predicate and re-derives the same handful of rules — don't leak
credentials, don't `rm -rf`, don't escape the workspace, don't call an
arbitrary host, don't loop forever.

These are those rules, as tested and versioned library code. Each returns an
ordinary `PolicySet`, so it composes with `&`/`|`/`~` and with your own
policies exactly like anything you'd write by hand.

    from tollgate import TollgateInterceptor
    from tollgate.policies import no_secrets_in_args, path_within, rate_limit_policy

    interceptor = TollgateInterceptor(policies=[
        no_secrets_in_args(),
        path_within(["/srv/workspace"], tool_names=("write_file",)),
        rate_limit_policy(50),
    ])

**Scope them.** A policy with no `active_when` runs against every tool through
the same interceptor. Each constructor here takes `tool_names` (or is scoped by
`tool_name`) for exactly that reason — see the "common pitfall" note in
CLAUDE.md. The argument-reading policies fail closed when the argument is
missing, so an unscoped policy blocks rather than silently passing, but that is
a backstop, not the intended configuration.

**They are seatbelts, not sandboxes.** `no_destructive_sql`/
`no_destructive_shell` pattern-match strings; an adversary in full control of
the input can evade them. They stop agent mistakes and opportunistic prompt
injection, and they are not a substitute for running untrusted code with real
OS-level isolation.
"""

from __future__ import annotations

from tollgate.policies.destructive import no_destructive_shell, no_destructive_sql
from tollgate.policies.limits import budget_policy, rate_limit_policy
from tollgate.policies.network import domain_allowlist, host_allowed
from tollgate.policies.paths import is_within, path_within
from tollgate.policies.secrets import find_secrets, no_secrets_in_args

__all__ = [
    "budget_policy",
    "domain_allowlist",
    "find_secrets",
    "host_allowed",
    "is_within",
    "no_destructive_shell",
    "no_destructive_sql",
    "no_secrets_in_args",
    "path_within",
    "rate_limit_policy",
]
