"""Shared fail-safe helper: a bug in user-supplied policy code must never
crash the tool call it's meant to guard.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def safe_call(fn: Callable[[Any], bool], ctx: Any) -> tuple[bool, str | None]:
    """Call `fn(ctx)`, catching any exception.

    Returns `(result, error)`: on success `error` is `None`; on exception,
    `result=False` (fail-closed) and `error` is a short description suitable
    for a `RuleResult.reason` suffix.
    """
    try:
        return bool(fn(ctx)), None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
