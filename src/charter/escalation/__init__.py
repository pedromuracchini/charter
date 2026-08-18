"""Real `EscalationHandler` implementations.

Unlike the framework adapters, none of these is auto-registered — there is no
way to detect that an agent wants Slack. Call
`charter.register_handler(scheme, handler)` explicitly; see
`examples/real_escalation_handlers.py`.
"""

from charter.escalation.cli import CLIEscalationHandler
from charter.escalation.slack import SlackEscalationHandler
from charter.escalation.webhook import WebhookEscalationHandler

__all__ = [
    "CLIEscalationHandler",
    "SlackEscalationHandler",
    "WebhookEscalationHandler",
]
