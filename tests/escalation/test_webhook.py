import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

import pytest

from chokepoint._scope import ExecutionScope
from chokepoint.core.context import GuardContext
from chokepoint.core.escalation import register_handler
from chokepoint.core.policy_set import PolicySet
from chokepoint.decisions import ESCALATE, GuardBlocked, RuleResult
from chokepoint.escalation.webhook import WebhookEscalationHandler

_URL = "https://example.invalid/approve"
_URLOPEN = "chokepoint.escalation.webhook.urllib.request.urlopen"


def _ctx(**args):
    return GuardContext.build(tool_name="transfer_funds", args=args, scope=ExecutionScope())


def _rule_result(**overrides):
    base = {
        "passed": False,
        "on_fail": ESCALATE,
        "reason": "large transfer",
        "policy_name": "p",
        "timeout_s": 5,
    }
    base.update(overrides)
    return RuleResult(**base)


def _response(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode("utf-8")
    resp.__enter__ = lambda self: resp
    resp.__exit__ = lambda self, *a: None
    return resp


def test_approved_response():
    handler = WebhookEscalationHandler(url=_URL)
    with patch(_URLOPEN, return_value=_response({"approved": True})):
        assert handler.escalate(_ctx(amount=100), _rule_result()) is True


def test_denied_response():
    handler = WebhookEscalationHandler(url=_URL)
    with patch(_URLOPEN, return_value=_response({"approved": False})):
        assert handler.escalate(_ctx(amount=9999), _rule_result()) is False


def test_missing_approved_field_denies():
    handler = WebhookEscalationHandler(url=_URL)
    with patch(_URLOPEN, return_value=_response({"status": "ok"})):
        assert handler.escalate(_ctx(), _rule_result()) is False


def test_non_json_response_denies():
    handler = WebhookEscalationHandler(url=_URL)
    resp = MagicMock()
    resp.read.return_value = b"not json"
    resp.__enter__ = lambda self: resp
    resp.__exit__ = lambda self, *a: None
    with patch(_URLOPEN, return_value=resp):
        assert handler.escalate(_ctx(), _rule_result()) is False


def test_network_error_denies():
    handler = WebhookEscalationHandler(url=_URL)
    with patch(_URLOPEN, side_effect=OSError("boom")):
        assert handler.escalate(_ctx(), _rule_result()) is False


def test_uses_min_of_handler_and_rule_timeout():
    handler = WebhookEscalationHandler(url=_URL, timeout_s=5)
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _response({"approved": True})

    with patch(_URLOPEN, side_effect=fake_urlopen):
        handler.escalate(_ctx(), _rule_result(timeout_s=1))
    assert captured["timeout"] == 1  # min(5, 1)

    with patch(_URLOPEN, side_effect=fake_urlopen):
        handler.escalate(_ctx(), _rule_result(timeout_s=30))
    assert captured["timeout"] == 5  # min(5, 30)


def test_headers_are_sent():
    handler = WebhookEscalationHandler(url=_URL, headers={"Authorization": "Bearer secret"})
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        return _response({"approved": True})

    with patch(_URLOPEN, side_effect=fake_urlopen):
        handler.escalate(_ctx(), _rule_result())
    # urllib.request.Request title-cases header keys
    assert captured["headers"].get("Authorization") == "Bearer secret"


class _ApprovalRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        approved = body["args"].get("amount", 0) < 1000
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"approved": approved}).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def local_approval_server():
    server = HTTPServer(("127.0.0.1", 0), _ApprovalRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_real_local_http_round_trip(local_approval_server):
    handler = WebhookEscalationHandler(url=f"http://127.0.0.1:{local_approval_server}/approve")
    assert handler.escalate(_ctx(amount=100), _rule_result()) is True
    assert handler.escalate(_ctx(amount=5000), _rule_result()) is False


def test_end_to_end_through_evaluate_call(local_approval_server):
    handler = WebhookEscalationHandler(url=f"http://127.0.0.1:{local_approval_server}/approve")
    register_handler("webhook-e2e-test", handler)

    policy = PolicySet("large_transfer")
    policy.require(
        lambda ctx: ctx.args["amount"] < 500,
        on_fail=ESCALATE,
        reason="large transfer",
        escalate_to="webhook-e2e-test://internal",
        timeout_s=5,
    )

    from chokepoint._engine import evaluate_call
    from chokepoint._scope import current_scope

    result = evaluate_call(
        tool_name="transfer_funds",
        args={"amount": 100},
        invoke=lambda: {"transferred": 100},
        policies=[policy],
        mode="enforce",
        scope=current_scope(),
    )
    assert result == {"transferred": 100}

    with pytest.raises(GuardBlocked):
        evaluate_call(
            tool_name="transfer_funds",
            args={"amount": 5000},
            invoke=lambda: {"transferred": 5000},
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )
