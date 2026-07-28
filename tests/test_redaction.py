"""Secrets and PII must not survive into anything Tollgate persists or sends."""

import pytest

from tollgate.core.interceptor import TollgateInterceptor
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK, GuardBlocked
from tollgate.ledger.ledger import ActionLedger
from tollgate.redaction import (
    DEFAULT_PLACEHOLDER,
    DEFAULT_SENSITIVE_KEYS,
    NullRedactor,
    PatternRedactor,
    configure_redaction,
    contains_placeholder,
    redact_args,
    redact_text,
)

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
ANTHROPIC_KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345"


def _noop(**kwargs):
    return kwargs


def _last_event():
    return ActionLedger.current().events()[-1]


# --- the default: secrets on, PII off --------------------------------------


def test_secrets_are_redacted_by_default():
    """A credential in the ledger has no upside — this is on out of the box."""
    interceptor = TollgateInterceptor(policies=[_always_allows()])
    interceptor.call("post", _noop, body=f"authorization: {AWS_KEY}")

    recorded = _last_event().args["body"]
    assert AWS_KEY not in recorded
    assert "REDACTED:aws_access_key_id" in recorded
    # Only the match is replaced; the surrounding text stays readable.
    assert recorded.startswith("authorization: ")


def test_pii_is_not_redacted_by_default():
    """Emails are routinely the point of the call — opting in is deliberate."""
    interceptor = TollgateInterceptor(policies=[_always_allows()])
    interceptor.call("send", _noop, to="alice@example.com")
    assert _last_event().args["to"] == "alice@example.com"


def test_pii_is_redacted_when_enabled():
    configure_redaction(include_pii=True)
    interceptor = TollgateInterceptor(policies=[_always_allows()])
    interceptor.call("send", _noop, to="alice@example.com", note="ssn 123-45-6789")

    event = _last_event()
    assert "alice@example.com" not in event.args["to"]
    assert "123-45-6789" not in event.args["note"]


def test_redaction_can_be_turned_off_entirely():
    configure_redaction(enabled=False)
    interceptor = TollgateInterceptor(policies=[_always_allows()])
    interceptor.call("post", _noop, body=AWS_KEY)
    assert _last_event().args["body"] == AWS_KEY


def _always_allows() -> PolicySet:
    policy = PolicySet("allows")
    policy.require(lambda ctx: True, on_fail=BLOCK, reason="fine")
    return policy


# --- policies still see the real values ------------------------------------


def test_policies_evaluate_against_unredacted_arguments():
    """The whole design rests on this: redact at record time, never before.
    A policy that checks a credential must be able to see it."""
    seen = {}
    policy = PolicySet("inspect")
    policy.require(
        lambda ctx: seen.update(body=ctx.args["body"]) is None,
        on_fail=BLOCK,
        reason="inspect",
    )
    interceptor = TollgateInterceptor(policies=[policy])
    interceptor.call("post", _noop, body=AWS_KEY)

    assert seen["body"] == AWS_KEY  # predicate saw the real value
    assert AWS_KEY not in _last_event().args["body"]  # ledger did not


def test_the_secret_detecting_policy_still_blocks_with_redaction_on():
    from tollgate.policies import no_secrets_in_args

    interceptor = TollgateInterceptor(policies=[no_secrets_in_args()])
    with pytest.raises(GuardBlocked, match="credential"):
        interceptor.call("post", _noop, body=AWS_KEY)

    # The blocking event itself must not carry the secret it blocked.
    assert AWS_KEY not in str(_last_event().args)


# --- everything the engine writes ------------------------------------------


def test_the_jsonl_sink_never_receives_the_raw_secret(tmp_path):
    sink = tmp_path / "ledger.jsonl"
    ActionLedger.configure(sink_path=sink)

    interceptor = TollgateInterceptor(policies=[_always_allows()])
    interceptor.call("post", _noop, body=ANTHROPIC_KEY)

    assert ANTHROPIC_KEY not in sink.read_text(encoding="utf-8")


def test_exports_never_contain_the_raw_secret():
    interceptor = TollgateInterceptor(policies=[_always_allows()])
    interceptor.call("post", _noop, body=AWS_KEY)

    ledger = ActionLedger.current()
    assert AWS_KEY not in ledger.export_compliance_report(format="json")
    assert AWS_KEY not in ledger.export_compliance_report(format="csv")


def test_a_reason_carrying_an_argument_value_is_redacted():
    """A fail-closed predicate folds its exception text into the reason, and
    that text routinely quotes the argument that broke it."""
    policy = PolicySet("breaks")
    policy.require(
        lambda ctx: ctx.args["missing"],  # KeyError quotes nothing sensitive...
        on_fail=BLOCK,
        reason="check",
    )
    interceptor = TollgateInterceptor(policies=[policy])
    with pytest.raises(GuardBlocked):
        interceptor.call("post", _noop, body=AWS_KEY)

    # ...but the args recorded alongside it must still be clean.
    assert AWS_KEY not in str(_last_event().args)


def test_a_raising_tool_does_not_leak_its_arguments():
    def boom(**kwargs):
        raise ValueError(f"bad token {AWS_KEY}")

    interceptor = TollgateInterceptor(policies=[])
    with pytest.raises(ValueError):
        interceptor.call("post", boom, body="x")

    event = _last_event()
    assert event.decision == "ERROR"
    assert AWS_KEY not in event.reason
    assert "REDACTED" in event.reason


def test_contributing_rule_reasons_are_redacted():
    policy = PolicySet("multi")
    policy.require(lambda ctx: False, on_fail=BLOCK, reason=f"saw {AWS_KEY}")
    policy.require(lambda ctx: False, on_fail=BLOCK, reason="second failure")

    interceptor = TollgateInterceptor(policies=[policy])
    with pytest.raises(GuardBlocked):
        interceptor.call("post", _noop, body="x")

    assert all(AWS_KEY not in r.reason for r in _last_event().contributing_rules)


# --- escalation messages ---------------------------------------------------


def test_the_escalation_summary_is_redacted():
    """Slack is the least controlled destination Tollgate writes to."""
    from tollgate._scope import ExecutionScope
    from tollgate.core.context import GuardContext
    from tollgate.decisions import ESCALATE, RuleResult
    from tollgate.escalation._message import format_escalation_summary

    ctx = GuardContext.build(
        tool_name="transfer", args={"key": AWS_KEY}, scope=ExecutionScope()
    )
    summary = format_escalation_summary(
        ctx, RuleResult(passed=False, on_fail=ESCALATE, reason="approve", policy_name="p")
    )
    assert AWS_KEY not in summary
    assert "REDACTED" in summary


def test_the_escalation_summary_is_truncated():
    """Slack rejects a message over 40k outright, so a large payload would
    turn every escalation on that tool into a silent delivery failure."""
    from tollgate._scope import ExecutionScope
    from tollgate.core.context import GuardContext
    from tollgate.decisions import ESCALATE, RuleResult
    from tollgate.escalation._message import MAX_ARGS_CHARS, format_escalation_summary

    ctx = GuardContext.build(
        tool_name="upload", args={"blob": "x" * 50_000}, scope=ExecutionScope()
    )
    summary = format_escalation_summary(
        ctx, RuleResult(passed=False, on_fail=ESCALATE, reason="approve", policy_name="p")
    )
    assert "truncated" in summary
    assert len(summary) < MAX_ARGS_CHARS + 500


# --- the redactor itself ---------------------------------------------------


def test_sensitive_keys_are_replaced_wholesale():
    """A field named `password` is a secret even when no pattern matches it."""
    out = redact_args({"password": "hunter2", "username": "alice"})
    assert out == {"password": DEFAULT_PLACEHOLDER, "username": "alice"}


@pytest.mark.parametrize("key", DEFAULT_SENSITIVE_KEYS)
def test_every_default_sensitive_key_is_honoured(key):
    assert redact_args({key: "value"})[key] == DEFAULT_PLACEHOLDER


def test_sensitive_key_matching_is_case_insensitive():
    assert redact_args({"API_KEY": "x"})["API_KEY"] == DEFAULT_PLACEHOLDER
    assert redact_args({"Authorization": "x"})["Authorization"] == DEFAULT_PLACEHOLDER


def test_nested_structures_are_walked():
    out = redact_args(
        {"payload": {"headers": [{"x": f"Bearer {AWS_KEY}"}], "meta": ("a", AWS_KEY)}}
    )
    assert AWS_KEY not in str(out)
    # Container types survive the round trip.
    assert isinstance(out["payload"]["headers"], list)
    assert isinstance(out["payload"]["meta"], tuple)


def test_non_string_values_pass_through_untouched():
    out = redact_args({"amount": 500, "ok": True, "ratio": 1.5, "nothing": None})
    assert out == {"amount": 500, "ok": True, "ratio": 1.5, "nothing": None}


def test_redaction_does_not_mutate_the_input():
    original = {"body": AWS_KEY, "nested": {"k": AWS_KEY}}
    redact_args(original)
    assert original["body"] == AWS_KEY
    assert original["nested"]["k"] == AWS_KEY


def test_ordinary_text_is_left_alone():
    for text in ["hello world", "SELECT * FROM users", "amount=500", ""]:
        assert redact_text(text) == text


def test_credit_cards_are_luhn_checked():
    """Without Luhn this would eat any 16-digit order number."""
    redactor = PatternRedactor(redact_credit_cards=True)
    valid = "4111 1111 1111 1111"  # a real Luhn-valid test number
    invalid = "1234 5678 9012 3456"

    assert "REDACTED:credit_card" in redactor.redact_text(f"card {valid}")
    assert invalid in redactor.redact_text(f"order {invalid}")


def test_custom_keys_replace_the_defaults():
    configure_redaction(keys=["mrn"])
    out = redact_args({"mrn": "12345", "password": "hunter2"})
    assert out["mrn"] == DEFAULT_PLACEHOLDER
    assert out["password"] == "hunter2"  # defaults were replaced, not extended


def test_extra_patterns_add_to_the_defaults():
    import re

    configure_redaction(extra_patterns=[("employee_id", re.compile(r"\bEMP-\d{5}\b"))])
    out = redact_args({"note": "EMP-12345 and " + AWS_KEY})
    assert "EMP-12345" not in out["note"]
    assert AWS_KEY not in out["note"]


def test_a_custom_redactor_can_be_installed():
    class Upper:
        def redact_args(self, args):
            return {k: "SCRUBBED" for k in args}

        def redact_text(self, text):
            return "SCRUBBED"

    configure_redaction(redactor=Upper())
    interceptor = TollgateInterceptor(policies=[_always_allows()])
    interceptor.call("post", _noop, body="anything")
    assert _last_event().args == {"body": "SCRUBBED"}


def test_null_redactor_is_a_pass_through():
    redactor = NullRedactor()
    assert redactor.redact_args({"a": AWS_KEY}) == {"a": AWS_KEY}
    assert redactor.redact_text(AWS_KEY) == AWS_KEY


def test_contains_placeholder_detects_nesting():
    assert contains_placeholder({"a": [{"b": DEFAULT_PLACEHOLDER}]})
    assert contains_placeholder("prefix [REDACTED:jwt] suffix")
    assert not contains_placeholder({"a": ["clean", 1, None]})


# --- replay and fixtures acknowledge redaction -----------------------------


def test_replay_flags_a_redacted_event():
    from tollgate.ledger.ledger import replay

    interceptor = TollgateInterceptor(policies=[_always_allows()])
    interceptor.call("post", _noop, body=AWS_KEY)

    result = replay(_last_event().event_id)
    assert result.redacted is True


def test_replay_of_a_clean_event_is_not_flagged():
    from tollgate.ledger.ledger import replay

    interceptor = TollgateInterceptor(policies=[_always_allows()])
    interceptor.call("post", _noop, body="nothing sensitive")

    assert replay(_last_event().event_id).redacted is False


def test_generated_fixtures_skip_redacted_events():
    """A test replaying placeholders could never pass; dropping it silently
    would overstate coverage instead."""
    from tollgate.testing.harness import fixtures_from_events

    interceptor = TollgateInterceptor(policies=[_always_allows()])
    interceptor.call("clean", _noop, body="fine")
    interceptor.call("dirty", _noop, body=AWS_KEY)

    source = fixtures_from_events(ActionLedger.current().events())
    compile(source, "<generated>", "exec")
    assert "pytest.mark.skip" in source
    assert "import pytest" in source
    # The clean event still produces a live test.
    assert source.count("def test_") == 2
    assert source.count("@pytest.mark.skip") == 1
