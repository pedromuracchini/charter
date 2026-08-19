"""`chokepoint` CLI: report, lint, replay, repl.

Agent modules are loaded as plain Python files and introspected for two
conventional module-level names: `POLICIES: list[Policy]` (required by
`report`/`lint`/`repl`) and, optionally, `REGISTRY: ChokepointRegistry` and
`TOOL_NAMES: list[str]` — see CLAUDE.md and `examples/clinical.py` for the
convention this CLI expects.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Literal

from pydantic import ValidationError

from chokepoint import __version__
from chokepoint.core.policy_set import Policy
from chokepoint.ledger.event import LedgerEvent
from chokepoint.ledger.ledger import ActionLedger, _events_to_csv
from chokepoint.ledger.ledger import replay as replay_event
from chokepoint.linter.linter import lint as lint_policies
from chokepoint.multiagent.registry import ChokepointRegistry
from chokepoint.report.graph import delegation_graph, policy_graph
from chokepoint.report.narrative import narrative
from chokepoint.report.policy_report import DEFAULT_WINDOW_HOURS, build_report
from chokepoint.testing.harness import fixtures_from_events
from chokepoint.testing.repl import run_repl


def _load_module(path: str) -> ModuleType:
    module_path = Path(path).resolve()
    # Checked before `spec_from_file_location`, which happily builds a spec for
    # a path that isn't there and only fails deep inside `exec_module` — with a
    # ten-frame importlib traceback instead of a one-line "no such file".
    if not module_path.is_file():
        raise SystemExit(f"no such agent module: {path!r}")
    spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"could not load module from {path!r}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _policies_from_module(module: ModuleType) -> list[Policy]:
    policies = getattr(module, "POLICIES", None)
    if policies is None:
        raise SystemExit(f"module {module.__name__!r} does not define a module-level POLICIES list")
    return list(policies)


def _load_ledger_events(ledger_path: str | None) -> list[LedgerEvent]:
    """Merge the in-memory ledger with events read back from a JSONL sink.

    A malformed line is skipped with a warning on stderr rather than aborting
    the whole read. The sink is an append-only log that a process can be killed
    partway through writing, so a truncated final line is an ordinary thing to
    find — refusing to report on 10,000 good events because the last one is
    half-written would make the tooling useless exactly when it is needed.
    """
    events = ActionLedger.current().events()
    if not ledger_path:
        return events

    parsed: list[LedgerEvent] = []
    skipped = 0
    try:
        with Path(ledger_path).open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    parsed.append(LedgerEvent.model_validate_json(line))
                except ValidationError:
                    skipped += 1
    except OSError as exc:
        # Same reasoning as the agent-module load path: a mistyped path is an
        # everyday CLI mistake and deserves one line, not a traceback.
        raise SystemExit(f"could not read ledger {ledger_path!r}: {exc.strerror}") from exc
    if skipped:
        print(
            f"warning: skipped {skipped} unparseable line(s) in {ledger_path}",
            file=sys.stderr,
        )
    return events + parsed


def _cmd_report(args: argparse.Namespace) -> None:
    module = _load_module(args.agent)
    policies = _policies_from_module(module)
    events = _load_ledger_events(args.ledger)

    if args.delegation:
        if args.format not in ("dot", "mermaid", "text"):
            # Silently emitting mermaid for `--format json` hid the fact that
            # delegation_graph() has no JSON writer.
            raise SystemExit(f"--delegation does not support --format {args.format}; use dot or mermaid")
        delegation_format: Literal["dot", "mermaid"] = "dot" if args.format == "dot" else "mermaid"
        print(delegation_graph(events, format=delegation_format))
        return
    if args.format in ("dot", "mermaid"):
        print(policy_graph(events, format=args.format))
        return

    tool_names = getattr(module, "TOOL_NAMES", None)
    report = build_report(
        policies,
        events,
        all_tool_names=tool_names,
        window_hours=args.window_hours,
        include_static=not args.no_static_coverage,
    )
    if args.format == "json":
        print(
            json.dumps(
                {
                    "window_hours": report.window_hours,
                    "coverage_ratio": report.coverage_ratio,
                    "covered_tools": list(report.covered_tools),
                    "uncovered_tools": list(report.uncovered_tools),
                    "policies": [asdict(p) for p in report.policies],
                },
                indent=2,
            )
        )
    else:
        total = len(report.covered_tools) + len(report.uncovered_tools)
        print(f"coverage: {report.coverage_ratio:.0%} ({len(report.covered_tools)}/{total} tools)")
        if report.uncovered_tools:
            print(f"uncovered tools: {', '.join(report.uncovered_tools)}")
        for stat in report.policies:
            print(
                f"- {stat.name} [{stat.policy_hash}] (last {report.window_hours:g}h) "
                f"block={stat.block_count} escalate={stat.escalate_count} allow={stat.allow_count} "
                f"tools={list(stat.tools_covered)}"
            )

    if args.fail_under is not None and report.coverage_ratio < args.fail_under:
        raise SystemExit(f"coverage {report.coverage_ratio:.0%} is below the required {args.fail_under:.0%}")


def _cmd_lint(args: argparse.Namespace) -> None:
    agent = args.agent or args.agent_positional
    if agent is None:
        raise SystemExit("lint requires --agent PATH")
    module = _load_module(agent)
    policies = _policies_from_module(module)
    registry: ChokepointRegistry | None = getattr(module, "REGISTRY", None)
    tool_names = getattr(module, "TOOL_NAMES", None)
    actions = getattr(module, "ACTIONS", None)
    findings = lint_policies(policies, tool_names=tool_names, registry=registry, actions=actions)
    if not findings:
        print("no issues found")
        return
    for finding in findings:
        print(f"[{finding.severity}] {finding.message}")
    if any(f.severity == "error" for f in findings):
        raise SystemExit(1)


def _cmd_replay(args: argparse.Namespace) -> None:
    policies = None
    if args.agent:
        module = _load_module(args.agent)
        policies = _policies_from_module(module)
    result = replay_event(args.event_id, policies=policies)
    print(f"original decision: {result.original_decision}")
    if result.new_results is not None:
        for r in result.new_results:
            mark = "PASS" if r.passed else "FAIL"
            print(f"  [{mark}] {r.policy_name}: {r.reason}")
        print(f"changed: {result.changed}")


def _cmd_repl(
    args: argparse.Namespace,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> None:
    """Load an agent module and hand its policies to the interactive REPL.

    `input_fn`/`output_fn` are forwarded to `run_repl` and default to the
    builtins; they exist so the subcommand can be driven from a test without a
    terminal (`argparse` only ever passes `args`).
    """
    module = _load_module(args.agent)
    policies = _policies_from_module(module)
    run_repl(policies, input_fn=input_fn, output_fn=output_fn)


def _cmd_export(args: argparse.Namespace) -> None:
    """Dump the ledger in one of its non-graph formats.

    `export_compliance_report`, `narrative()` and `fixtures_from_events()` all
    existed but had no CLI command, so the only way to reach them was to write
    a Python script.
    """
    events = _load_ledger_events(args.ledger)
    if args.format == "json":
        output = json.dumps([e.model_dump() for e in events], indent=2)
    elif args.format == "csv":
        output = _events_to_csv(events)
    elif args.format == "narrative":
        output = narrative(events, audience=args.audience)
    else:
        output = fixtures_from_events(events)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"wrote {len(events)} event(s) to {args.output}", file=sys.stderr)
    else:
        print(output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chokepoint",
        description="Inspect, lint and replay Chokepoint policies.",
        epilog=(
            "An agent file is loaded as a plain Python module and read for a "
            "module-level POLICIES list (plus optional REGISTRY / TOOL_NAMES / "
            "ACTIONS). NOTE: this executes the file — only point it at code you trust."
        ),
    )
    parser.add_argument("--version", action="version", version=f"chokepoint {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    report = subparsers.add_parser("report", help="inspect an agent's registered policies")
    report.add_argument("--agent", required=True)
    report.add_argument(
        "--fail-under",
        type=float,
        default=None,
        metavar="RATIO",
        help="exit non-zero if tool coverage is below this ratio (e.g. 0.8) — for CI",
    )
    report.add_argument("--format", choices=["text", "json", "dot", "mermaid"], default="text")
    report.add_argument("--delegation", action="store_true")
    report.add_argument("--ledger", default=None, help="optional JSONL ledger sink file to include")
    report.add_argument(
        "--window-hours",
        type=float,
        default=DEFAULT_WINDOW_HOURS,
        help=f"recency window for per-policy activation counts (default {DEFAULT_WINDOW_HOURS:g}h)",
    )
    report.add_argument(
        "--no-static-coverage",
        action="store_true",
        help="only count a tool as covered if it has recorded ledger activity (audit-only view)",
    )
    report.set_defaults(func=_cmd_report)

    lint = subparsers.add_parser("lint", help="lint an agent's registered policies")
    # `--agent` everywhere: this used to be the one positional, which meant
    # `chokepoint lint x.py` but `chokepoint report --agent x.py`. The positional
    # is still accepted so existing invocations keep working.
    lint.add_argument("--agent", default=None)
    lint.add_argument("agent_positional", nargs="?", default=None, help=argparse.SUPPRESS)
    lint.set_defaults(func=_cmd_lint)

    replay = subparsers.add_parser("replay", help="replay a ledger event")
    replay.add_argument("event_id")
    replay.add_argument("--agent", default=None)
    replay.set_defaults(func=_cmd_replay)

    repl = subparsers.add_parser("repl", help="interactive policy REPL")
    repl.add_argument("--agent", required=True)
    repl.set_defaults(func=_cmd_repl)

    export = subparsers.add_parser("export", help="dump the ledger for audit or test generation")
    export.add_argument(
        "--format",
        choices=["json", "csv", "narrative", "fixtures"],
        default="json",
        help="fixtures emits a runnable pytest module built from recorded decisions",
    )
    export.add_argument("--ledger", default=None, help="JSONL ledger sink file to read")
    export.add_argument("--output", default=None, metavar="PATH", help="write here instead of stdout")
    export.add_argument(
        "--audience",
        choices=["technical", "non-technical"],
        default="non-technical",
        help="narrative format only",
    )
    export.set_defaults(func=_cmd_export)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
