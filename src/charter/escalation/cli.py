"""`CLIEscalationHandler` — local human-in-the-loop approval via the terminal.

No network I/O, no credentials — useful for local development, demos, and
CLI-driven agents where a person is already watching the terminal. Not
suited to a deployed production agent with no one at a keyboard; see
`charter.escalation.slack`/`webhook` for that.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from charter.core.context import GuardContext
from charter.core.escalation import EscalationHandler
from charter.decisions import RuleResult
from charter.escalation._message import format_escalation_summary

logger = logging.getLogger("charter.escalation.cli")


class CLIEscalationHandler(EscalationHandler):
    """Print the escalation and prompt for an approve/deny answer via `input_fn`.

    `timeout_s`, if set, is **informational only** — printed in the prompt
    ("Approve within {timeout_s:.0f}s?") but not actually enforced here:
    stdlib `input()` has no clean, portable way to be interrupted mid-call.
    The real cutoff still comes from the engine abandoning the blocked call
    once `rule_result.timeout_s` elapses, same as any other handler.
    """

    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        approve_words: Iterable[str] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.input_fn = input_fn
        self.approve_words = set(approve_words) if approve_words is not None else {"y", "yes", "approve"}
        self.timeout_s = timeout_s

    def escalate(self, ctx: GuardContext, rule_result: RuleResult) -> bool:
        print("=== Charter escalation ===")
        print(format_escalation_summary(ctx, rule_result))

        prompt = "Approve? [y/N]: "
        if self.timeout_s is not None:
            prompt = f"Approve within {self.timeout_s:.0f}s? [y/N]: "

        try:
            response = self.input_fn(prompt)
        except (EOFError, KeyboardInterrupt):
            logger.warning("no response from CLI escalation prompt — denying (fail-safe)")
            return False

        approved = response.strip().lower() in self.approve_words
        if not approved:
            logger.info("CLI escalation denied (response=%r)", response)
        return approved
