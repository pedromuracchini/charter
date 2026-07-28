"""`WebhookEscalationHandler` — approve/deny via a single synchronous HTTP call.

POSTs a JSON payload describing the escalation to a configured URL and
expects an immediate JSON response with an `"approved"` boolean — suited to
an internal approval service, an automated policy engine, or anything else
that can answer synchronously (unlike Slack, which needs polling — see
`tollgate.escalation.slack`). Uses only `urllib.request` — no new dependency.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from tollgate.core.context import GuardContext
from tollgate.core.escalation import EscalationHandler
from tollgate.decisions import RuleResult

logger = logging.getLogger("tollgate.escalation.webhook")


class WebhookEscalationHandler(EscalationHandler):
    """POST an escalation to `url`, expecting a JSON `{"approved": bool}` response.

    `headers` is where the caller puts their own authentication (a shared
    secret, a bearer token, ...) — per `SECURITY.md`, Tollgate has no opinion
    on how you authenticate the endpoint; make sure `url` is trusted. The
    effective request timeout is `min(self.timeout_s, rule_result.timeout_s)`.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.url = url
        self.headers = dict(headers) if headers else {}
        self.timeout_s = timeout_s

    def escalate(self, ctx: GuardContext, rule_result: RuleResult) -> bool:
        payload = {
            "tool_name": ctx.tool_name,
            "args": ctx.args,
            "reason": rule_result.reason,
            "policy_name": rule_result.policy_name,
            "severity": rule_result.severity,
            "caller_agent_id": ctx.caller_agent_id,
            "caller_role": ctx.caller_role,
            "session_id": ctx.session_id,
        }
        timeout = min(self.timeout_s, rule_result.timeout_s)
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={**self.headers, "Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.error(
                "webhook escalation request failed: %s: %s — denying (fail-safe)", type(exc).__name__, exc
            )
            return False

        if not isinstance(body, dict) or "approved" not in body:
            logger.error(
                "webhook escalation response missing 'approved' field: %r — denying (fail-safe)", body
            )
            return False
        return bool(body["approved"])
