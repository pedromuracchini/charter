"""Confine a tool's filesystem argument to a set of allowed roots.

Stops the classic escape: an agent that builds a path from model output and
reaches `../../etc/passwd`, or follows a symlink out of its sandbox.

Resolution is the whole point. `Path.resolve()` normalises `..` segments *and*
follows symlinks, so the check runs against the real target rather than the
string the agent produced — comparing raw strings is what makes naive
allowlists trivially bypassable.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from tollgate.core.context import GuardContext
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK, Decision, Severity


def is_within(candidate: str | Path, roots: Iterable[str | Path]) -> bool:
    """Whether `candidate` resolves to a location inside one of `roots`.

    Fails closed: an unresolvable path (a broken symlink loop, a permission
    error while resolving) returns `False` rather than being waved through.
    """
    try:
        resolved = Path(candidate).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
        except (OSError, RuntimeError, ValueError):
            continue
        else:
            return True
    return False


def path_within(
    roots: Iterable[str | Path],
    *,
    arg: str = "path",
    tool_names: Iterable[str] | None = None,
    name: str = "path_within_allowed_roots",
    on_fail: Decision = BLOCK,
    severity: Severity = "high",
    escalate_to: str | None = None,
) -> PolicySet:
    """Require `ctx.args[arg]` to resolve inside one of `roots`.

    A missing argument fails closed — an unscoped policy that cannot find the
    path it is meant to check has not verified anything.

        path_within(["/srv/agent-workspace"], tool_names=("write_file", "read_file"))
    """
    allowed_roots = [Path(r) for r in roots]
    allowed_tools = set(tool_names) if tool_names is not None else None
    policy = PolicySet(
        name,
        active_when=(lambda ctx: ctx.tool_name in allowed_tools) if allowed_tools is not None else None,
    )

    def _check(ctx: GuardContext) -> bool:
        candidate = ctx.args.get(arg)
        if not isinstance(candidate, str | Path):
            return False
        return is_within(candidate, allowed_roots)

    policy.require(
        _check,
        on_fail=on_fail,
        reason=f"{arg} resolves outside the allowed roots ({', '.join(str(r) for r in allowed_roots)})",
        severity=severity,
        escalate_to=escalate_to,
    )
    return policy
