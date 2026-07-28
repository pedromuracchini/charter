"""Real `EscalationHandler` implementations.

Unlike the framework adapters, none of these is auto-registered — there is no
way to detect that an agent wants Slack. Call
`tollgate.register_handler(scheme, handler)` explicitly; see
`examples/real_escalation_handlers.py`.
"""

from tollgate.escalation.cli import CLIEscalationHandler
from tollgate.escalation.slack import SlackEscalationHandler
from tollgate.escalation.webhook import WebhookEscalationHandler

__all__ = [
    "CLIEscalationHandler",
    "SlackEscalationHandler",
    "WebhookEscalationHandler",
]
