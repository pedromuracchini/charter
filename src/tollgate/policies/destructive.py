"""Reject destructive SQL and shell commands in tool arguments.

The archetypal agent failure: a `run_sql` or `run_shell` tool handed a
generated string that drops a table or wipes a directory. Both rules default to
BLOCK, but `on_fail=ESCALATE` is often the better fit — plenty of legitimate
work involves a `DELETE`.

These are pattern matchers over a command string, not parsers. They stop the
obvious cases; an adversary who controls the string exactly can evade them.
Treat them as a seatbelt against agent mistakes, not as a sandbox.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from tollgate.core.context import GuardContext
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK, Decision, Severity

#: Statements that drop or truncate rather than modify.
_SQL_STRUCTURE = re.compile(r"\b(?:DROP|TRUNCATE)\s+(?:TABLE|DATABASE|SCHEMA|INDEX|VIEW)\b", re.I)
#: `DELETE FROM x` / `UPDATE x SET ...` with no WHERE clause — an unbounded write.
_SQL_UNBOUNDED_DELETE = re.compile(r"\bDELETE\s+FROM\b(?!.*\bWHERE\b)", re.I | re.S)
_SQL_UNBOUNDED_UPDATE = re.compile(r"\bUPDATE\b.*?\bSET\b(?!.*\bWHERE\b)", re.I | re.S)

_SHELL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("recursive delete", re.compile(r"\brm\s+(?:-[A-Za-z]*\s+)*-{1,2}[A-Za-z]*[rR]", re.I)),
    ("filesystem format", re.compile(r"\bmkfs(?:\.\w+)?\b", re.I)),
    ("raw disk write", re.compile(r"\bdd\b[^\n]*\bof=/dev/", re.I)),
    ("device redirect", re.compile(r">\s*/dev/(?:sd|nvme|hd)\w+")),
    ("fork bomb", re.compile(r":\s*\(\s*\)\s*\{.*\|.*&\s*\}\s*;")),
    ("history wipe", re.compile(r"\bhistory\s+-c\b", re.I)),
    ("recursive chmod/chown at root", re.compile(r"\bch(?:mod|own)\s+(?:-[A-Za-z]*\s+)*-R\b[^\n]*\s/\s*$")),
    ("shutdown", re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b", re.I)),
)


def _arg_getter(arg: str) -> Callable[[GuardContext], str]:
    """Read one named argument as a string, tolerating its absence.

    An unscoped policy sees every tool through the same interceptor, and a tool
    without this argument must not KeyError into a fail-safe BLOCK — the
    "common pitfall" in CLAUDE.md. Prefer scoping with `tool_names` anyway.
    """
    return lambda ctx: str(ctx.args.get(arg, ""))


def no_destructive_sql(
    *,
    arg: str = "query",
    tool_names: tuple[str, ...] | None = None,
    name: str = "no_destructive_sql",
    on_fail: Decision = BLOCK,
    severity: Severity = "high",
    escalate_to: str | None = None,
    allow_unbounded_writes: bool = False,
) -> PolicySet:
    """Reject `DROP`/`TRUNCATE`, and unbounded `DELETE`/`UPDATE`, in `ctx.args[arg]`.

    Set `allow_unbounded_writes=True` to permit a `DELETE`/`UPDATE` with no
    `WHERE` clause and only reject structural drops.

        no_destructive_sql(tool_names=("run_sql",), on_fail=ESCALATE,
                           escalate_to="slack://dba")
    """
    read = _arg_getter(arg)
    allowed = set(tool_names) if tool_names is not None else None
    policy = PolicySet(
        name,
        active_when=(lambda ctx: ctx.tool_name in allowed) if allowed is not None else None,
    )
    policy.require(
        lambda ctx: not _SQL_STRUCTURE.search(read(ctx)),
        on_fail=on_fail,
        reason=f"{arg} drops or truncates a database object",
        severity=severity,
        escalate_to=escalate_to,
    )
    if not allow_unbounded_writes:
        policy.require(
            lambda ctx: (
                not (_SQL_UNBOUNDED_DELETE.search(read(ctx)) or _SQL_UNBOUNDED_UPDATE.search(read(ctx)))
            ),
            on_fail=on_fail,
            reason=f"{arg} is a DELETE or UPDATE with no WHERE clause",
            severity=severity,
            escalate_to=escalate_to,
        )
    return policy


def no_destructive_shell(
    *,
    arg: str = "command",
    tool_names: tuple[str, ...] | None = None,
    name: str = "no_destructive_shell",
    on_fail: Decision = BLOCK,
    severity: Severity = "high",
    escalate_to: str | None = None,
) -> PolicySet:
    """Reject `rm -rf`, `mkfs`, `dd of=/dev/...`, shutdown and friends in `ctx.args[arg]`.

    no_destructive_shell(tool_names=("run_shell",))
    """
    read = _arg_getter(arg)
    allowed = set(tool_names) if tool_names is not None else None
    policy = PolicySet(
        name,
        active_when=(lambda ctx: ctx.tool_name in allowed) if allowed is not None else None,
    )
    for label, pattern in _SHELL_PATTERNS:
        policy.require(
            # `pattern=pattern` binds per iteration; a bare closure over the
            # loop variable would leave every rule checking the last pattern.
            lambda ctx, pattern=pattern: not pattern.search(read(ctx)),  # type: ignore[misc]
            on_fail=on_fail,
            reason=f"{arg} contains a destructive shell operation ({label})",
            severity=severity,
            escalate_to=escalate_to,
        )
    return policy
