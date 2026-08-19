import argparse
import io
import json

import pytest

from chokepoint.cli.main import _cmd_repl, _load_ledger_events, main
from chokepoint.core.interceptor import ChokepointInterceptor
from chokepoint.core.policy_set import PolicySet
from chokepoint.decisions import BLOCK, GuardBlocked
from chokepoint.ledger.event import LedgerEvent
from chokepoint.ledger.ledger import ActionLedger

AGENT_MODULE = """
from chokepoint import PolicySet, BLOCK

policy = PolicySet("smoke_policy")
policy.require(lambda ctx: ctx.args.get("x", 0) > 0, on_fail=BLOCK, reason="x must be positive")

POLICIES = [policy]
TOOL_NAMES = ["do_thing", "do_other_thing"]
"""


def _write_agent(tmp_path):
    path = tmp_path / "agent.py"
    path.write_text(AGENT_MODULE)
    return str(path)


def test_report_text_format(tmp_path, capsys):
    main(["report", "--agent", _write_agent(tmp_path)])
    out = capsys.readouterr().out
    assert "coverage:" in out
    assert "smoke_policy" in out


def test_report_json_format(tmp_path, capsys):
    main(["report", "--agent", _write_agent(tmp_path), "--format", "json"])
    out = capsys.readouterr().out
    assert '"coverage_ratio"' in out


def test_report_mermaid_format(tmp_path, capsys):
    main(["report", "--agent", _write_agent(tmp_path), "--format", "mermaid"])
    out = capsys.readouterr().out
    assert "graph LR" in out


def test_lint_reports_uncovered_tools(tmp_path, capsys):
    main(["lint", _write_agent(tmp_path)])
    out = capsys.readouterr().out
    assert "warning" in out or "no issues found" in out


def test_replay_without_agent(tmp_path, capsys):
    from chokepoint.ledger.event import LedgerEvent

    ActionLedger.current().record(
        LedgerEvent(
            event_id="evt_cli",
            ts="2026-06-03T14:32:01Z",
            tool="t",
            args={},
            policy="p",
            decision="BLOCK",
            reason="r",
        )
    )
    main(["replay", "evt_cli"])
    out = capsys.readouterr().out
    assert "original decision: BLOCK" in out


def test_version_flag(capsys):
    import chokepoint

    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert chokepoint.__version__ in capsys.readouterr().out


def test_lint_accepts_agent_as_a_flag(capsys):
    main(["lint", "--agent", "examples/clinical.py"])
    assert "tool" in capsys.readouterr().out


def test_lint_still_accepts_the_positional_form(capsys):
    """The old invocation must keep working."""
    main(["lint", "examples/clinical.py"])
    assert "tool" in capsys.readouterr().out


def test_lint_without_an_agent_is_an_explicit_error():
    with pytest.raises(SystemExit, match="requires --agent"):
        main(["lint"])


def test_report_fail_under_exits_non_zero_below_the_threshold(capsys):
    with pytest.raises(SystemExit, match="below the required"):
        main(["report", "--agent", "examples/clinical.py", "--fail-under", "0.99"])
    # The report itself is still printed, so CI logs show what failed.
    assert "coverage:" in capsys.readouterr().out


def test_report_fail_under_passes_above_the_threshold(capsys):
    main(["report", "--agent", "examples/clinical.py", "--fail-under", "0.0"])
    assert "coverage:" in capsys.readouterr().out


def test_delegation_with_an_unsupported_format_errors_instead_of_lying():
    """It used to silently emit mermaid for --format json."""
    with pytest.raises(SystemExit, match="does not support --format json"):
        main(["report", "--agent", "examples/clinical.py", "--delegation", "--format", "json"])


def test_report_json_serializes_policy_stats(capsys):
    main(["report", "--agent", "examples/clinical.py", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert "coverage_ratio" in payload
    assert all("policy_hash" in p for p in payload["policies"])


def _record_two_events():
    policy = PolicySet("exported")
    policy.require(lambda ctx: ctx.args.get("ok", False), on_fail=BLOCK, reason="not ok")
    interceptor = ChokepointInterceptor(policies=[policy], agent_id="exporter")
    interceptor.call("t", lambda **kw: None, ok=True)
    with pytest.raises(GuardBlocked):
        interceptor.call("t", lambda **kw: None, ok=False)


@pytest.mark.parametrize("fmt", ["json", "csv", "narrative", "fixtures"])
def test_export_produces_output_in_every_format(fmt, capsys):
    _record_two_events()
    main(["export", "--format", fmt])
    assert capsys.readouterr().out.strip()


def test_export_writes_to_a_file(tmp_path, capsys):
    _record_two_events()
    target = tmp_path / "audit.json"
    main(["export", "--format", "json", "--output", str(target)])

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert len(payload) == 2
    assert "wrote 2 event(s)" in capsys.readouterr().err


def test_export_fixtures_emits_importable_python(tmp_path, capsys):
    _record_two_events()
    main(["export", "--format", "fixtures"])
    source = capsys.readouterr().out
    compile(source, "<generated>", "exec")  # must be valid Python
    assert "def test_" in source


def test_export_fixtures_skips_events_recorded_with_redacted_args(capsys):
    """Secrets are redacted at record time, so the generated test could only
    ever replay placeholders — it is emitted skipped rather than dropped."""
    policy = PolicySet("exported")
    policy.require(lambda ctx: True, on_fail=BLOCK, reason="fine")
    ChokepointInterceptor(policies=[policy], agent_id="exporter").call(
        "login", lambda **kw: None, password="hunter2"
    )
    main(["export", "--format", "fixtures"])

    source = capsys.readouterr().out
    assert "@pytest.mark.skip" in source
    compile(source, "<generated>", "exec")


def _jsonl_event(event_id="evt_file", tool="t", decision="BLOCK", chain=()):
    return LedgerEvent(
        event_id=event_id,
        ts="2026-06-03T14:32:01Z",
        tool=tool,
        args={},
        policy="smoke_policy",
        decision=decision,
        reason="r",
        delegation_chain=list(chain),
    ).model_dump_json()


def _write_ledger_file(tmp_path, *lines):
    path = tmp_path / "ledger.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_load_ledger_events_skips_unparseable_lines(tmp_path, capsys):
    """A sink is an append-only log a process can die partway through writing,
    so a truncated final line must not abort the whole read."""
    truncated = _jsonl_event(event_id="evt_bad")[:20]
    path = _write_ledger_file(
        tmp_path,
        _jsonl_event(event_id="evt_good_1"),
        "not json at all",
        truncated,
        _jsonl_event(event_id="evt_good_2"),
    )

    events = _load_ledger_events(path)

    assert [e.event_id for e in events] == ["evt_good_1", "evt_good_2"]
    assert "skipped 2 unparseable line(s)" in capsys.readouterr().err


def test_load_ledger_events_is_silent_when_every_line_parses(tmp_path, capsys):
    path = _write_ledger_file(tmp_path, _jsonl_event(), "", _jsonl_event(event_id="evt_2"))

    events = _load_ledger_events(path)

    assert len(events) == 2
    assert capsys.readouterr().err == ""


def test_load_ledger_events_merges_the_in_memory_ledger(tmp_path):
    ActionLedger.current().record(LedgerEvent.model_validate_json(_jsonl_event(event_id="evt_mem")))
    path = _write_ledger_file(tmp_path, _jsonl_event(event_id="evt_disk"))

    assert [e.event_id for e in _load_ledger_events(path)] == ["evt_mem", "evt_disk"]


def test_load_ledger_events_without_a_path_returns_the_in_memory_ledger():
    ActionLedger.current().record(LedgerEvent.model_validate_json(_jsonl_event(event_id="evt_mem")))
    assert [e.event_id for e in _load_ledger_events(None)] == ["evt_mem"]


def test_report_with_a_ledger_file_counts_its_events(tmp_path, capsys):
    path = _write_ledger_file(tmp_path, _jsonl_event(tool="do_thing"))
    main(["report", "--agent", _write_agent(tmp_path), "--ledger", path, "--format", "json"])

    payload = json.loads(capsys.readouterr().out)
    stats = {p["name"]: p for p in payload["policies"]}
    assert "do_thing" in stats["smoke_policy"]["tools_covered"]


def test_report_with_a_corrupt_ledger_file_still_reports(tmp_path, capsys):
    path = _write_ledger_file(tmp_path, _jsonl_event(tool="do_thing"), "{truncated")
    main(["report", "--agent", _write_agent(tmp_path), "--ledger", path])

    captured = capsys.readouterr()
    assert "coverage:" in captured.out
    assert "skipped 1 unparseable line(s)" in captured.err


def test_export_with_a_corrupt_ledger_file_still_exports(tmp_path, capsys):
    path = _write_ledger_file(tmp_path, _jsonl_event(), "]]not json[[")
    main(["export", "--format", "json", "--ledger", path])

    captured = capsys.readouterr()
    assert len(json.loads(captured.out)) == 1
    assert "skipped 1 unparseable line(s)" in captured.err


def test_report_delegation_dot_format(tmp_path, capsys):
    path = _write_ledger_file(tmp_path, _jsonl_event(chain=("orchestrator", "executor")))
    main(["report", "--agent", _write_agent(tmp_path), "--ledger", path, "--delegation", "--format", "dot"])

    out = capsys.readouterr().out
    assert out.startswith("digraph delegation {")
    assert "orchestrator" in out


def test_report_delegation_defaults_to_mermaid(tmp_path, capsys):
    path = _write_ledger_file(tmp_path, _jsonl_event(chain=("orchestrator", "executor")))
    main(["report", "--agent", _write_agent(tmp_path), "--ledger", path, "--delegation"])

    assert "graph LR" in capsys.readouterr().out


def test_report_no_static_coverage_is_audit_only(tmp_path, capsys):
    main(["report", "--agent", _write_agent(tmp_path), "--no-static-coverage", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["covered_tools"] == []


def test_replay_with_an_agent_re_evaluates_the_policies(tmp_path, capsys):
    ActionLedger.current().record(
        LedgerEvent(
            event_id="evt_replay",
            ts="2026-06-03T14:32:01Z",
            tool="do_thing",
            args={"x": 5},
            policy="smoke_policy",
            decision="BLOCK",
            reason="r",
        )
    )
    main(["replay", "evt_replay", "--agent", _write_agent(tmp_path)])

    out = capsys.readouterr().out
    assert "original decision: BLOCK" in out
    assert "[PASS] smoke_policy: x must be positive" in out
    # x=5 now passes, so the stored BLOCK is no longer reproduced.
    assert "changed: True" in out


def test_loading_a_module_that_does_not_exist_exits(tmp_path):
    """It used to fail ten frames deep inside importlib instead."""
    with pytest.raises(SystemExit, match="no such agent module"):
        main(["report", "--agent", str(tmp_path / "no_such_agent.py")])


def test_loading_a_file_python_cannot_import_exits(tmp_path):
    path = tmp_path / "agent.txt"
    path.write_text("POLICIES = []\n")
    with pytest.raises(SystemExit, match="could not load module"):
        main(["report", "--agent", str(path)])


def test_a_module_without_policies_exits_with_an_explicit_message(tmp_path):
    path = tmp_path / "no_policies.py"
    path.write_text("X = 1\n")
    with pytest.raises(SystemExit, match="does not define a module-level POLICIES list"):
        main(["report", "--agent", str(path)])


def test_lint_exits_non_zero_on_an_error_severity_finding(tmp_path, capsys):
    """A scoped policy with no registry silently leaves caller_role None — the
    CI gate is the exit code, not the printed text."""
    path = tmp_path / "scoped_agent.py"
    path.write_text(
        "from chokepoint import AgentScopedPolicy, BLOCK\n"
        "policy = AgentScopedPolicy(\n"
        '    name="scoped", allowed_roles=["executor"], on_fail=BLOCK, reason="r",\n'
        ")\n"
        "POLICIES = [policy]\n"
    )
    with pytest.raises(SystemExit) as excinfo:
        main(["lint", "--agent", str(path)])

    assert excinfo.value.code == 1
    assert "[error]" in capsys.readouterr().out


def test_lint_of_a_module_without_policies_exits(tmp_path):
    path = tmp_path / "no_policies_lint.py"
    path.write_text("X = 1\n")
    with pytest.raises(SystemExit, match="does not define a module-level POLICIES list"):
        main(["lint", "--agent", str(path)])


class _ScriptedInput:
    """Feeds queued lines to `run_repl`, then raises EOFError like a closed stdin."""

    def __init__(self, lines):
        self._lines = list(lines)

    def __call__(self, prompt: str) -> str:
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


def test_repl_subcommand_evaluates_a_line_and_exits_on_quit(tmp_path):
    printed: list[str] = []
    args = argparse.Namespace(agent=_write_agent(tmp_path))
    _cmd_repl(
        args,
        input_fn=_ScriptedInput(["do_thing", '{"x": -1}', "quit"]),
        output_fn=printed.append,
    )

    assert any("decision: BLOCK" in line for line in printed)


def test_repl_subcommand_rejects_a_module_without_policies(tmp_path):
    path = tmp_path / "no_policies_repl.py"
    path.write_text("X = 1\n")
    with pytest.raises(SystemExit, match="does not define a module-level POLICIES list"):
        _cmd_repl(argparse.Namespace(agent=str(path)), input_fn=_ScriptedInput([]), output_fn=lambda _: None)


def test_repl_subcommand_defaults_to_stdin_and_stdout(tmp_path, monkeypatch, capsys):
    """`main()` passes only `args`, so the builtins must remain the defaults."""
    monkeypatch.setattr("sys.stdin", io.StringIO('do_thing\n{"x": 1}\n'))
    main(["repl", "--agent", _write_agent(tmp_path)])

    out = capsys.readouterr().out
    assert "chokepoint policy REPL" in out
    assert "decision: ALLOW (no rule fired)" in out
