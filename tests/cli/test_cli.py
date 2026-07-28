import json

import pytest

from tollgate.cli.main import main
from tollgate.core.interceptor import TollgateInterceptor
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK, GuardBlocked
from tollgate.ledger.ledger import ActionLedger

AGENT_MODULE = '''
from tollgate import PolicySet, BLOCK

policy = PolicySet("smoke_policy")
policy.require(lambda ctx: ctx.args.get("x", 0) > 0, on_fail=BLOCK, reason="x must be positive")

POLICIES = [policy]
TOOL_NAMES = ["do_thing", "do_other_thing"]
'''


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
    from tollgate.ledger.event import LedgerEvent

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
    import tollgate

    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert tollgate.__version__ in capsys.readouterr().out


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
    interceptor = TollgateInterceptor(policies=[policy], agent_id="exporter")
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
