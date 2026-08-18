"""`SlackEscalationHandler` — approve/deny by polling for a reaction.

Slack has no built-in synchronous "click here, get the answer back in this
HTTP response" primitive without also running a public webhook receiver, so
this uses the practical alternative: post a message, then poll for a ✅/❌
reaction from an authorized approver — needing only a Slack bot token
(`chat:write` and `reactions:read` scopes), no inbound webhook server.

Uses only `urllib.request` from the standard library — no `slack-sdk` or
other new dependency.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from charter.core.context import GuardContext
from charter.core.escalation import EscalationHandler
from charter.decisions import RuleResult
from charter.errors import ConfigurationError, EscalationError
from charter.escalation._message import format_escalation_summary

logger = logging.getLogger("charter.escalation.slack")

_API_BASE = "https://slack.com/api"


class SlackEscalationHandler(EscalationHandler):
    """Post an escalation to a Slack channel and poll for an approve/deny
    reaction from an allowlisted approver.

    `approvers` (a set of Slack user IDs) is **required, not optional** —
    without an allowlist, *anyone* in the channel reacting with the approve
    emoji would approve the action, which is exactly the vulnerability
    `SECURITY.md` warns custom handlers to avoid. `timeout_s` is how long
    *this bot* waits for a reaction, independent of any given rule's own
    `timeout_s` — the effective wait is `min(self.timeout_s, rule_result.timeout_s)`.
    """

    def __init__(
        self,
        bot_token: str,
        channel: str,
        approvers: set[str],
        timeout_s: float = 300.0,
        approve_emoji: str = "white_check_mark",
        deny_emoji: str = "x",
        poll_interval_s: float = 2.0,
    ) -> None:
        if not approvers:
            raise ConfigurationError("SlackEscalationHandler requires a non-empty approvers set")
        self.bot_token = bot_token
        self.channel = channel
        self.approvers = approvers
        self.timeout_s = timeout_s
        self.approve_emoji = approve_emoji
        self.deny_emoji = deny_emoji
        self.poll_interval_s = poll_interval_s

    def _call(self, method: str, http_method: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.bot_token}"}
        if http_method == "POST":
            req = urllib.request.Request(
                f"{_API_BASE}/{method}",
                data=json.dumps(params).encode("utf-8"),
                headers={**headers, "Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
        else:
            query = urllib.parse.urlencode(params)
            req = urllib.request.Request(f"{_API_BASE}/{method}?{query}", headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return dict(json.loads(resp.read().decode("utf-8")))

    def _post_message(self, text: str) -> str:
        resp = self._call("chat.postMessage", "POST", {"channel": self.channel, "text": text})
        if not resp.get("ok"):
            raise EscalationError(f"chat.postMessage failed: {resp.get('error')}")
        ts = resp.get("ts")
        if not ts:
            raise EscalationError(f"chat.postMessage response had no ts: {resp!r}")
        return str(ts)

    def _check_reaction(self, ts: str) -> bool | None:
        """Return `True`/`False` once an allowlisted approver has reacted,
        else `None` (keep polling)."""
        resp = self._call("reactions.get", "GET", {"channel": self.channel, "timestamp": ts})
        if not resp.get("ok"):
            if resp.get("error") == "no_reaction":
                return None
            raise EscalationError(f"reactions.get failed: {resp.get('error')}")

        reactions = resp.get("message", {}).get("reactions", [])
        approved_by: set[str] = set()
        denied_by: set[str] = set()
        for reaction in reactions:
            users = set(reaction.get("users", [])) & self.approvers
            if reaction.get("name") == self.approve_emoji:
                approved_by |= users
            elif reaction.get("name") == self.deny_emoji:
                denied_by |= users

        if denied_by:
            return False
        if approved_by:
            return True
        return None

    def escalate(self, ctx: GuardContext, rule_result: RuleResult) -> bool:
        deadline_s = min(self.timeout_s, rule_result.timeout_s)
        try:
            ts = self._post_message(format_escalation_summary(ctx, rule_result))
        except Exception as exc:
            logger.error("failed to post Slack escalation message: %s: %s", type(exc).__name__, exc)
            return False

        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            try:
                decision = self._check_reaction(ts)
            except Exception as exc:
                logger.error("failed to poll Slack reactions: %s: %s", type(exc).__name__, exc)
                return False
            if decision is not None:
                return decision
            time.sleep(min(self.poll_interval_s, max(0.0, deadline - time.monotonic())))

        logger.warning(
            "Slack escalation timed out after %.1fs with no reaction — denying (fail-safe)", deadline_s
        )
        return False
