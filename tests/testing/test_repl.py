import inspect

import pytest

from charter.core.policy_set import PolicySet
from charter.decisions import BLOCK
from charter.testing.repl import evaluate_synthetic, run_repl


def test_evaluate_synthetic_no_policies_allows():
    trace = evaluate_synthetic("t", {}, [])
    assert trace.decision is None
    assert trace.results == ()


def test_evaluate_synthetic_reports_failing_rule():
    policy = PolicySet("p")
    policy.require(lambda ctx: ctx.args.get("x", 0) > 0, on_fail=BLOCK, reason="x must be positive")
    trace = evaluate_synthetic("t", {"x": -1}, [policy], caller_role="executor")
    assert trace.decision is not None
    assert trace.decision.on_fail is BLOCK
    assert trace.context.caller_role == "executor"


class _ScriptedInput:
    """Feeds queued lines to the REPL, then raises EOFError like a closed stdin."""

    def __init__(self, lines):
        self._lines = list(lines)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


def _positive_x_policy():
    policy = PolicySet("p")
    policy.require(lambda ctx: ctx.args.get("x", 0) > 0, on_fail=BLOCK, reason="x must be positive")
    return policy


def _drive(lines, policies=None):
    printed: list[str] = []
    run_repl(policies if policies is not None else [], _ScriptedInput(lines), printed.append)
    return printed


def test_run_repl_prints_a_banner_and_exits_on_quit():
    printed = _drive(["quit"])
    assert printed == ["charter policy REPL — type a tool name, or 'quit' to exit."]


def test_run_repl_evaluates_a_json_line_and_prints_the_decision():
    printed = _drive(["t", '{"x": -1}', "quit"], [_positive_x_policy()])
    assert "  [FAIL] p: x must be positive" in printed
    assert "decision: BLOCK — x must be positive" in printed


def test_run_repl_reports_allow_when_no_rule_fires():
    printed = _drive(["t", '{"x": 1}', "quit"], [_positive_x_policy()])
    assert "  [PASS] p: x must be positive" in printed
    assert "decision: ALLOW (no rule fired)" in printed


def test_run_repl_treats_an_empty_args_line_as_an_empty_object():
    printed = _drive(["t", "", "quit"], [_positive_x_policy()])
    assert "decision: BLOCK — x must be positive" in printed


def test_run_repl_reports_malformed_json_and_keeps_looping():
    """A typo must not end the session — the next line is still evaluated."""
    printed = _drive(["t", "{not json", "t", '{"x": 1}', "quit"], [_positive_x_policy()])
    assert any(line.startswith("invalid input:") for line in printed)
    assert "decision: ALLOW (no rule fired)" in printed


def test_run_repl_exits_cleanly_on_eof():
    printed = _drive([])
    assert printed == ["charter policy REPL — type a tool name, or 'quit' to exit."]


def test_run_repl_exits_cleanly_on_eof_at_the_args_prompt():
    """EOF mid-entry is reported, and the next tool prompt then hits EOF too."""
    printed = _drive(["t"], [_positive_x_policy()])
    assert any(line.startswith("invalid input:") for line in printed)


@pytest.mark.parametrize("line", ["quit", "exit", "", "   "])
def test_run_repl_ends_the_loop_on_every_exit_word(line):
    scripted = _ScriptedInput([line])
    run_repl([], scripted, lambda _: None)
    # Only the tool prompt was ever shown — the loop never asked for args.
    assert scripted.prompts == ["tool> "]


def test_run_repl_defaults_to_the_builtins():
    """Passing neither parameter must leave the public behavior identical."""
    params = inspect.signature(run_repl).parameters
    assert params["input_fn"].default is input
    assert params["output_fn"].default is print
