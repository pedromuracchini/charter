"""The public API surface is part of the contract — check it holds together."""

import importlib
import inspect

import pytest

import charter

#: A frozen snapshot of everything `import charter` promises. Adding a name
#: here is a deliberate act; a name disappearing from it is a breaking change
#: for someone. Asserting only that `__all__` was *sorted* let both happen
#: silently, which is the wrong guarantee for a package heading to 1.0.
#:
#: Ordering matches ruff's RUF022 (all-caps constants first, then classes, then
#: functions), which is what `ruff check --fix` enforces on the source.
PUBLIC_API = [
    "ALLOW",
    "ALL_TOOLS",
    "BLOCK",
    "ESCALATE",
    "PII_PATTERNS",
    "SECRET_PATTERNS",
    "ActionLedger",
    "AdapterError",
    "AgentIdentity",
    "AgentScopedPolicy",
    "AndPolicy",
    "CLIEscalationHandler",
    "CallState",
    "CharterError",
    "CharterInterceptor",
    "CharterRegistry",
    "CharterWarning",
    "ConfigurationError",
    "ConfigurationWarning",
    "ContributingRule",
    "Decision",
    "EscalationError",
    "EscalationHandler",
    "ExecutionScope",
    "FailSafeEscalationHandler",
    "GuardBlocked",
    "GuardContext",
    "GuardDecision",
    "Hook",
    "IrreversibilityLevel",
    "LedgerError",
    "LedgerEvent",
    "LedgerEventNotFound",
    "LintFinding",
    "LintSeverity",
    "Mode",
    "NotPolicy",
    "NullRedactor",
    "OrPolicy",
    "PatternRedactor",
    "Policy",
    "PolicyReport",
    "PolicySet",
    "Redactor",
    "ReplayResult",
    "ReversibleAction",
    "RuleResult",
    "Severity",
    "SlackEscalationHandler",
    "WebhookEscalationHandler",
    "__version__",
    "build_report",
    "configure_ledger",
    "configure_otel",
    "configure_redaction",
    "current_redactor",
    "current_scope",
    "delegation_depth",
    "extend_chain",
    "guard",
    "lint",
    "max_delegation_depth_policy",
    "pick_decision",
    "policies",
    "register_handler",
    "registered_handlers",
    "replay",
    "reset_handlers",
    "reset_ledger",
    "reset_otel",
    "reset_redaction",
    "session",
    "unregister_handler",
    "wrap",
]

_KIND_MARKER = {
    inspect.Parameter.POSITIONAL_ONLY: "/",
    inspect.Parameter.KEYWORD_ONLY: "*",
    inspect.Parameter.VAR_POSITIONAL: "*",
    inspect.Parameter.VAR_KEYWORD: "**",
}


def signature_shape(func) -> str:
    """Parameter names, kinds and defaults — the part callers depend on.

    Annotations are deliberately excluded: adding or refining a type hint is
    not a breaking change, while renaming a parameter, reordering two, or
    making one keyword-only silently breaks working code.
    """
    parts = []
    for parameter in inspect.signature(func).parameters.values():
        marker = _KIND_MARKER.get(parameter.kind, "")
        default = "" if parameter.default is inspect.Parameter.empty else f"={parameter.default!r}"
        parts.append(f"{marker}{parameter.name}{default}")
    return ", ".join(parts)


#: Shapes of the entry points most user code is written against. A silent
#: rename or reordering here breaks callers without breaking any test that only
#: checks the name still resolves.
PINNED_SIGNATURES = {
    "guard": (
        "pre=None, post=None, *on_fail, *reason, *escalate_to=None, *timeout_s=300, *severity='medium'"
    ),
    "wrap": "agent, interceptor",
    "replay": "event_id, policies=None, hook='pre'",
    "configure_ledger": "*sink_path=None, *max_events=10000",
    "configure_redaction": (
        "*enabled=True, *keys=None, *include_pii=False, *redact_credit_cards=None, "
        "*extra_patterns=(), *placeholder='[REDACTED]', *redactor=None"
    ),
    "configure_otel": "tracer_provider=None, allow_sample_rate=1.0, block_sample_rate=1.0",
    "lint": "policies, tool_names=None, registry=None, actions=None",
    "session": "**overrides",
    "pick_decision": "failing",
}

#: Every sub-package that re-exports something. `from charter.ledger import
#: ActionLedger` used to fail outright: these files were all 0 bytes.
SUBPACKAGES = [
    "charter.core",
    "charter.ledger",
    "charter.escalation",
    "charter.report",
    "charter.linter",
    "charter.testing",
    "charter.multiagent",
    "charter.otel",
    "charter.cli",
    "charter.policies",
]


def test_every_name_in_all_actually_exists():
    missing = [name for name in charter.__all__ if not hasattr(charter, name)]
    assert missing == []


def test_public_api_matches_the_snapshot():
    """Fails on any addition or removal, so neither happens by accident.

    Update `PUBLIC_API` in the same commit that changes the surface — the diff
    on this list is the reviewable record of an API change.
    """
    assert list(charter.__all__) == PUBLIC_API


@pytest.mark.parametrize("name", sorted(PINNED_SIGNATURES))
def test_entry_point_signatures_are_stable(name):
    assert signature_shape(getattr(charter, name)) == PINNED_SIGNATURES[name]


def test_interceptor_call_keeps_tool_name_and_func_positional_only():
    """They are positional-only so a tool argument may be named `tool_name` or
    `func` without colliding — a regression here silently breaks such tools."""
    parameters = inspect.signature(charter.CharterInterceptor.call).parameters
    for name in ("tool_name", "func"):
        assert parameters[name].kind is inspect.Parameter.POSITIONAL_ONLY


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
    assert hasattr(charter, name)


def test_importing_charter_does_not_pull_in_a_framework_sdk():
    """Adapters import their framework lazily, inside `applies_to()`, so
    `import charter` stays cheap and works with none of the extras installed.

    `opentelemetry` is deliberately excluded: `otel/config.py` probes it at
    import time (guarded by try/except) to set `OTEL_AVAILABLE`. It is a light
    API-only package and the probe is how the no-op degradation works.
    """
    import subprocess
    import sys

    code = (
        "import sys, charter\n"
        "eager = [m for m in ('langchain_core', 'langgraph', 'agents', 'mcp') if m in sys.modules]\n"
        "assert not eager, f'imported eagerly: {eager}'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_version_is_exposed():
    assert isinstance(charter.__version__, str)
    assert charter.__version__
