from tollgate.cli.main import main
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
