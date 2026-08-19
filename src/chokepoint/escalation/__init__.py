"""Real `EscalationHandler` implementations.

Unlike the framework adapters, none of these is auto-registered — there is no
way to detect that an agent wants Slack. Call
`chokepoint.register_handler(scheme, handler)` explicitly; see
`examples/real_escalation_handlers.py`.
"""

from chokepoint.escalation.cli import CLIEscalationHandler
from chokepoint.escalation.slack import SlackEscalationHandler
from chokepoint.escalation.webhook import WebhookEscalationHandler

__all__ = [
    "CLIEscalationHandler",
    "SlackEscalationHandler",
    "WebhookEscalationHandler",
]
