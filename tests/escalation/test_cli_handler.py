import pytest

from chokepoint._scope import ExecutionScope
from chokepoint.core.context import GuardContext
from chokepoint.core.escalation import register_handler
from chokepoint.core.policy_set import PolicySet
from chokepoint.decisions import ESCALATE, GuardBlocked, RuleResult
from chokepoint.escalation.cli import CLIEscalationHandler


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


def _scripted(*responses):
    it = iter(responses)
    return lambda prompt: next(it)


@pytest.mark.parametrize("response", ["y", "yes", "approve", "Y", "YES", "  yes  "])
def test_approve_words_are_case_and_whitespace_insensitive(response):
    handler = CLIEscalationHandler(input_fn=_scripted(response))
    assert handler.escalate(_ctx(), _rule_result()) is True


@pytest.mark.parametrize("response", ["n", "no", "garbage", ""])
def test_anything_else_denies(response):
    handler = CLIEscalationHandler(input_fn=_scripted(response))
    assert handler.escalate(_ctx(), _rule_result()) is False


def test_eof_denies():
    def raise_eof(prompt):
        raise EOFError

    handler = CLIEscalationHandler(input_fn=raise_eof)
    assert handler.escalate(_ctx(), _rule_result()) is False


def test_keyboard_interrupt_denies():
    def raise_interrupt(prompt):
        raise KeyboardInterrupt

    handler = CLIEscalationHandler(input_fn=raise_interrupt)
    assert handler.escalate(_ctx(), _rule_result()) is False


def test_custom_approve_words():
    handler = CLIEscalationHandler(input_fn=_scripted("go"), approve_words={"go"})
    assert handler.escalate(_ctx(), _rule_result()) is True


def test_timeout_note_appears_in_prompt_when_set():
    captured = {}

    def capture(prompt):
        captured["prompt"] = prompt
        return "y"

    handler = CLIEscalationHandler(input_fn=capture, timeout_s=42)
    handler.escalate(_ctx(), _rule_result())
    assert "42" in captured["prompt"]


def test_no_timeout_note_when_unset():
    captured = {}

    def capture(prompt):
        captured["prompt"] = prompt
        return "y"

    handler = CLIEscalationHandler(input_fn=capture)
    handler.escalate(_ctx(), _rule_result())
    assert captured["prompt"] == "Approve? [y/N]: "


def test_end_to_end_through_evaluate_call():
    handler = CLIEscalationHandler(input_fn=_scripted("y"))
    register_handler("cli-e2e-test", handler)

    policy = PolicySet("needs_approval")
    policy.require(
        lambda ctx: False,
        on_fail=ESCALATE,
        reason="always escalates",
        escalate_to="cli-e2e-test://local",
        timeout_s=5,
    )

    from chokepoint._engine import evaluate_call
    from chokepoint._scope import current_scope

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
    handler = CLIEscalationHandler(input_fn=_scripted("n"))
    register_handler("cli-deny-e2e-test", handler)

    policy = PolicySet("needs_approval")
    policy.require(
        lambda ctx: False,
        on_fail=ESCALATE,
        reason="always escalates",
        escalate_to="cli-deny-e2e-test://local",
        timeout_s=5,
    )

    from chokepoint._engine import evaluate_call
    from chokepoint._scope import current_scope

    with pytest.raises(GuardBlocked):
        evaluate_call(
            tool_name="delete_prod_db",
            args={"id": 1},
            invoke=lambda: {"ok": True},
            policies=[policy],
            mode="enforce",
            scope=current_scope(),
        )
