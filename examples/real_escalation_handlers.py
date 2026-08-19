"""Three real `EscalationHandler` implementations, back to back — each a
genuinely different approval-channel shape:

- `SlackEscalationHandler`: post a message, poll for a ✅/❌ reaction from an
  allowlisted approver. This section monkeypatches `urllib.request.urlopen`
  to simulate the Slack API in-process (clearly marked below) so the example
  is runnable without a real Slack workspace — swap in a real
  `SLACK_BOT_TOKEN` and channel ID in production and delete the monkeypatch.
- `WebhookEscalationHandler`: one synchronous HTTP POST, expects a JSON
  `{"approved": bool}` response. This section runs a real tiny local HTTP
  server (`http.server`, localhost-only) — a genuine round trip, no mocking.
- `CLIEscalationHandler`: local human-in-the-loop, via a scripted
  `input_fn` here so it doesn't need real interactive stdin.

Run directly:

    uv run python examples/real_escalation_handlers.py
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

from chokepoint import (
    ESCALATE,
    CLIEscalationHandler,
    GuardBlocked,
    SlackEscalationHandler,
    WebhookEscalationHandler,
    guard,
    register_handler,
)


def slack_section() -> None:
    print("\n=== Slack (mocked Slack API — see docstring) ===")

    def fake_response(body: dict) -> MagicMock:
        resp = MagicMock()
        resp.read.return_value = json.dumps(body).encode("utf-8")
        resp.__enter__ = lambda self: resp
        resp.__exit__ = lambda self, *a: None
        return resp

    calls = {"n": 0}

    def fake_urlopen(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return fake_response({"ok": True, "ts": "1.1"})
        # Simulate an approver reacting with ✅ on the first poll.
        return fake_response(
            {"ok": True, "message": {"reactions": [{"name": "white_check_mark", "users": ["U_APPROVER"]}]}}
        )

    handler = SlackEscalationHandler(
        bot_token="xoxb-demo",  # real usage: os.environ["SLACK_BOT_TOKEN"]
        channel="C_FINANCE_APPROVALS",
        approvers={"U_APPROVER"},
        timeout_s=60,
        poll_interval_s=0.05,
    )
    register_handler("slack", handler)

    @guard(
        pre=lambda ctx: ctx.args["amount"] < 500,
        on_fail=ESCALATE,
        escalate_to="slack://C_FINANCE_APPROVALS",
        timeout_s=5,
        reason="large transfer requires manual approval",
    )
    def transfer_funds(amount: float, to: str) -> dict:
        return {"transferred": amount, "to": to}

    with patch("chokepoint.escalation.slack.urllib.request.urlopen", side_effect=fake_urlopen):
        print(transfer_funds(amount=2000, to="alice"))


def webhook_section() -> None:
    print("\n=== Webhook (real local HTTP server) ===")

    class ApprovalHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            body = json.loads(self.rfile.read(length))
            approved = body["args"].get("amount", 0) < 1000
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"approved": approved}).encode())

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), ApprovalHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        handler = WebhookEscalationHandler(url=f"http://127.0.0.1:{server.server_port}/approve")
        register_handler("webhook", handler)

        @guard(
            pre=lambda ctx: ctx.args["amount"] < 500,
            on_fail=ESCALATE,
            escalate_to="webhook://internal-approval-service",
            timeout_s=5,
            reason="large withdrawal requires approval",
        )
        def withdraw(amount: float) -> dict:
            return {"withdrawn": amount}

        print(withdraw(amount=700))
        try:
            withdraw(amount=5000)
        except GuardBlocked as exc:
            print(f"blocked: {exc.decision.reason}")
    finally:
        server.shutdown()
        thread.join(timeout=2)


def cli_section() -> None:
    print("\n=== CLI (scripted input, no real stdin needed) ===")

    responses = iter(["y", "n"])
    handler = CLIEscalationHandler(input_fn=lambda prompt: next(responses), timeout_s=30)
    register_handler("cli", handler)

    @guard(
        pre=lambda ctx: ctx.args["path"] != "/etc/passwd",
        on_fail=ESCALATE,
        escalate_to="cli://local-operator",
        timeout_s=30,
        reason="sensitive file access requires local approval",
    )
    def read_file(path: str) -> dict:
        return {"path": path, "contents": "..."}

    print(read_file(path="/etc/passwd"))  # first scripted response: "y"
    try:
        read_file(path="/etc/passwd")  # second scripted response: "n"
    except GuardBlocked as exc:
        print(f"blocked: {exc.decision.reason}")


def main() -> None:
    slack_section()
    webhook_section()
    cli_section()


if __name__ == "__main__":
    main()
