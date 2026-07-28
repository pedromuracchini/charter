"""The public API surface is part of the contract — check it holds together."""

import importlib

import pytest

import tollgate

#: Every sub-package that re-exports something. `from tollgate.ledger import
#: ActionLedger` used to fail outright: these files were all 0 bytes.
SUBPACKAGES = [
    "tollgate.core",
    "tollgate.ledger",
    "tollgate.escalation",
    "tollgate.report",
    "tollgate.linter",
    "tollgate.testing",
    "tollgate.multiagent",
    "tollgate.otel",
    "tollgate.cli",
    "tollgate.policies",
]


def test_every_name_in_all_actually_exists():
    missing = [name for name in tollgate.__all__ if not hasattr(tollgate, name)]
    assert missing == []


def test_all_is_sorted():
    """Keeps diffs small and makes a missing entry easy to spot."""
    assert tollgate.__all__ == sorted(tollgate.__all__)


@pytest.mark.parametrize("module_name", SUBPACKAGES)
def test_subpackages_re_export_their_names(module_name):
    module = importlib.import_module(module_name)
    assert getattr(module, "__all__", None), f"{module_name} exports nothing"
    missing = [name for name in module.__all__ if not hasattr(module, name)]
    assert missing == []


@pytest.mark.parametrize(
    "name",
    [
        # Type aliases users need to annotate their own policy code, none of
        # which were reachable without a private-looking deep import.
        "Hook",
        "Mode",
        "Severity",
        "IrreversibilityLevel",
        "ExecutionScope",
        "CallState",
        "FailSafeEscalationHandler",
        "ContributingRule",
        "pick_decision",
        "lint",
        "build_report",
        "configure_ledger",
    ],
)
def test_previously_unexported_names_are_reachable(name):
    assert hasattr(tollgate, name)


def test_importing_tollgate_does_not_pull_in_a_framework_sdk():
    """Adapters import their framework lazily, inside `applies_to()`, so
    `import tollgate` stays cheap and works with none of the extras installed.

    `opentelemetry` is deliberately excluded: `otel/config.py` probes it at
    import time (guarded by try/except) to set `OTEL_AVAILABLE`. It is a light
    API-only package and the probe is how the no-op degradation works.
    """
    import subprocess
    import sys

    code = (
        "import sys, tollgate\n"
        "eager = [m for m in ('langchain_core', 'langgraph', 'agents', 'mcp') if m in sys.modules]\n"
        "assert not eager, f'imported eagerly: {eager}'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_version_is_exposed():
    assert isinstance(tollgate.__version__, str)
    assert tollgate.__version__
