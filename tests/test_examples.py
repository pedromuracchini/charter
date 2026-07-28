"""Every file in `examples/` is advertised as runnable, by the README and by
CLAUDE.md, and each one is a `uv run python examples/x.py` invocation in the
docs. Nothing else in the suite executes them, so the onboarding path was kept
working by manual discipline alone.
"""

import runpy
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLE_PATHS = sorted(EXAMPLES_DIR.glob("*.py"))

#: Examples whose module-level imports need an optional extra. The LangGraph
#: and OpenAI Agents examples both degrade to a printed hint when the extra is
#: absent, so running them proves nothing without it either.
REQUIRES_MODULE = {
    "langgraph_integration.py": "langchain_core",
    "openai_agents_integration.py": "agents",
    "mcp_integration.py": "mcp",
}

#: Examples that are not safe to run inside the unit suite, with the reason.
NOT_RUNNABLE_HERE = {
    "real_escalation_handlers.py": (
        "binds a local HTTP socket and patches urllib.request globally; the "
        "handlers themselves are covered by tests/escalation/"
    ),
}


def test_the_examples_directory_is_not_empty():
    """A glob that quietly matches nothing would turn this whole file green."""
    assert len(EXAMPLE_PATHS) > 10


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=lambda p: p.name)
def test_example_runs_without_raising(path, capsys):
    # The autouse fixture in tests/conftest.py resets the ledger, OTEL,
    # redaction and the handler/adapter registries around each parametrised
    # case, so one example cannot leak process-global state into the next.
    if path.name in NOT_RUNNABLE_HERE:
        pytest.skip(NOT_RUNNABLE_HERE[path.name])
    module = REQUIRES_MODULE.get(path.name)
    if module is not None:
        pytest.importorskip(module)

    runpy.run_path(str(path), run_name="__main__")

    # Every example demonstrates something by printing it; silence means the
    # `if __name__ == "__main__"` block never ran.
    assert capsys.readouterr().out
