import json
import time
from unittest.mock import MagicMock, patch

import pytest

from charter._scope import ExecutionScope
from charter.core.context import GuardContext
from charter.core.escalation import register_handler
from charter.core.policy_set import PolicySet
from charter.decisions import ESCALATE, GuardBlocked, RuleResult
from charter.escalation.slack import SlackEscalationHandler


def _ctx():
    return GuardContext.build(tool_name="delete_prod_db", args={"id": 1}, scope=ExecutionScope())


def _rule_result(**overrides):
    base = {
        "passed": False,
        "on_fail": ESCALATE,
        "reason": "dangerous action",
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


def _first_then_repeat(first: MagicMock, rest: MagicMock):
    """A `urlopen` side_effect: return `first` once (the chat.postMessage
    call), then `rest` for every subsequent call (each reactions.get poll)."""
    calls = {"n": 0}

    def side_effect(*args, **kwargs):
        calls["n"] += 1
        return first if calls["n"] == 1 else rest

    return side_effect


def test_requires_non_empty_approvers():
    with pytest.raises(ValueError):
        SlackEscalationHandler(bot_token="x", channel="C1", approvers=set())


def test_approved_by_allowlisted_approver():
    handler = SlackEscalationHandler(
        bot_token="xoxb-test", channel="C1", approvers={"U_OK"}, poll_interval_s=0.01
    )
    responses = [
        _response({"ok": True, "ts": "1.1"}),
        _response({"ok": True, "message": {"reactions": [{"name": "white_check_mark", "users": ["U_OK"]}]}}),
    ]
    with patch(
        "charter.escalation.slack.urllib.request.urlopen", side_effect=lambda *a, **kw: responses.pop(0)
    ):
        assert handler.escalate(_ctx(), _rule_result()) is True


def test_denied_by_deny_reaction():
    handler = SlackEscalationHandler(
        bot_token="xoxb-test", channel="C1", approvers={"U_OK"}, poll_interval_s=0.01
    )
    responses = [
        _response({"ok": True, "ts": "1.1"}),
        _response({"ok": True, "message": {"reactions": [{"name": "x", "users": ["U_OK"]}]}}),
    ]
    with patch(
        "charter.escalation.slack.urllib.request.urlopen", side_effect=lambda *a, **kw: responses.pop(0)
    ):
        assert handler.escalate(_ctx(), _rule_result()) is False


def test_reaction_from_non_approver_does_not_count():
    handler = SlackEscalationHandler(
        bot_token="xoxb-test", channel="C1", approvers={"U_OK"}, timeout_s=0.2, poll_interval_s=0.02
    )
    post = _response({"ok": True, "ts": "1.1"})
    stranger_reacted = _response(
        {"ok": True, "message": {"reactions": [{"name": "white_check_mark", "users": ["U_STRANGER"]}]}}
    )
    with patch(
        "charter.escalation.slack.urllib.request.urlopen",
        side_effect=_first_then_repeat(post, stranger_reacted),
    ):
        assert handler.escalate(_ctx(), _rule_result(timeout_s=5)) is False


def test_timeout_with_no_reaction_denies_promptly():
    handler = SlackEscalationHandler(
        bot_token="xoxb-test", channel="C1", approvers={"U_OK"}, timeout_s=0.2, poll_interval_s=0.02
    )
    post = _response({"ok": True, "ts": "1.1"})
    no_reaction = _response({"ok": False, "error": "no_reaction"})

    start = time.perf_counter()
    with patch(
        "charter.escalation.slack.urllib.request.urlopen", side_effect=_first_then_repeat(post, no_reaction)
    ):
        result = handler.escalate(_ctx(), _rule_result(timeout_s=5))
    elapsed = time.perf_counter() - start

    assert result is False
    assert elapsed < 1.0  # bounded by handler's own timeout_s=0.2, not rule_result's timeout_s=5


def test_rule_result_timeout_can_be_shorter_than_handler_timeout():
    handler = SlackEscalationHandler(
        bot_token="xoxb-test", channel="C1", approvers={"U_OK"}, timeout_s=30, poll_interval_s=0.02
    )
    post = _response({"ok": True, "ts": "1.1"})
    no_reaction = _response({"ok": False, "error": "no_reaction"})

    start = time.perf_counter()
    with patch(
        "charter.escalation.slack.urllib.request.urlopen", side_effect=_first_then_repeat(post, no_reaction)
    ):
        result = handler.escalate(_ctx(), _rule_result(timeout_s=0.2))
    elapsed = time.perf_counter() - start

    assert result is False
    assert elapsed < 1.0  # bounded by rule_result's timeout_s=0.2, not handler's timeout_s=30


def test_network_error_denies():
    handler = SlackEscalationHandler(bot_token="xoxb-test", channel="C1", approvers={"U_OK"})
    with patch("charter.escalation.slack.urllib.request.urlopen", side_effect=OSError("boom")):
        assert handler.escalate(_ctx(), _rule_result()) is False


def test_end_to_end_through_evaluate_call():
    handler = SlackEscalationHandler(
        bot_token="xoxb-test", channel="C1", approvers={"U_OK"}, poll_interval_s=0.01
    )
    register_handler("slack", handler)

    policy = PolicySet("needs_approval")
    policy.require(
        lambda ctx: False, on_fail=ESCALATE, reason="always escalates", escalate_to="slack://C1", timeout_s=5
    )

    from charter._engine import evaluate_call
    from charter._scope import current_scope

    responses = [
        _response({"ok": True, "ts": "1.1"}),
        _response({"ok": True, "message": {"reactions": [{"name": "white_check_mark", "users": ["U_OK"]}]}}),
    ]
    with patch(
        "charter.escalation.slack.urllib.request.urlopen", side_effect=lambda *a, **kw: responses.pop(0)
    ):
        result = evaluate_call(
            tool_name="delete_prod_db",
            args={"id": 1},
            invoke=lambda: {"ok": True},
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )
    assert result == {"ok": True}


def test_end_to_end_denied_raises_guard_blocked():
    handler = SlackEscalationHandler(
        bot_token="xoxb-test", channel="C1", approvers={"U_OK"}, poll_interval_s=0.01
    )
    register_handler("slack-deny-test", handler)

    policy = PolicySet("needs_approval")
    policy.require(
        lambda ctx: False,
        on_fail=ESCALATE,
        reason="always escalates",
        escalate_to="slack-deny-test://C1",
        timeout_s=5,
    )

    from charter._engine import evaluate_call
    from charter._scope import current_scope

    responses = [
        _response({"ok": True, "ts": "1.1"}),
        _response({"ok": True, "message": {"reactions": [{"name": "x", "users": ["U_OK"]}]}}),
    ]
    with (
        patch(
            "charter.escalation.slack.urllib.request.urlopen", side_effect=lambda *a, **kw: responses.pop(0)
        ),
        pytest.raises(GuardBlocked),
    ):
        evaluate_call(
            tool_name="delete_prod_db",
            args={"id": 1},
            invoke=lambda: {"ok": True},
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )
