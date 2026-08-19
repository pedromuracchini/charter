"""Secrets and PII must not survive into anything Chokepoint persists or sends."""

import copy
import time
from collections.abc import Mapping

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from chokepoint.core.interceptor import ChokepointInterceptor
from chokepoint.core.policy_set import PolicySet
from chokepoint.decisions import BLOCK, GuardBlocked
from chokepoint.ledger.ledger import ActionLedger
from chokepoint.redaction import (
    DEFAULT_PLACEHOLDER,
    DEFAULT_SENSITIVE_KEYS,
    SECRET_PATTERNS,
    NullRedactor,
    PatternRedactor,
    _is_luhn_valid,
    configure_redaction,
    contains_placeholder,
    redact_args,
    redact_text,
)

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
ANTHROPIC_KEY = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345"
VALID_CARD = "4111 1111 1111 1111"


def _noop(**kwargs):
    return kwargs


def _last_event():
    return ActionLedger.current().events()[-1]


# --- the default: secrets on, PII off --------------------------------------


def test_secrets_are_redacted_by_default():
    """A credential in the ledger has no upside — this is on out of the box."""
    interceptor = ChokepointInterceptor(policies=[_always_allows()])
    interceptor.call("post", _noop, body=f"authorization: {AWS_KEY}")

    recorded = _last_event().args["body"]
    assert AWS_KEY not in recorded
    assert "REDACTED:aws_access_key_id" in recorded
    # Only the match is replaced; the surrounding text stays readable.
    assert recorded.startswith("authorization: ")


def test_pii_is_not_redacted_by_default():
    """Emails are routinely the point of the call — opting in is deliberate."""
    interceptor = ChokepointInterceptor(policies=[_always_allows()])
    interceptor.call("send", _noop, to="alice@example.com")
    assert _last_event().args["to"] == "alice@example.com"


def test_pii_is_redacted_when_enabled():
    configure_redaction(include_pii=True)
    interceptor = ChokepointInterceptor(policies=[_always_allows()])
    interceptor.call("send", _noop, to="alice@example.com", note="ssn 123-45-6789")

    event = _last_event()
    assert "alice@example.com" not in event.args["to"]
    assert "123-45-6789" not in event.args["note"]


def test_redaction_can_be_turned_off_entirely():
    configure_redaction(enabled=False)
    interceptor = ChokepointInterceptor(policies=[_always_allows()])
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
    interceptor = ChokepointInterceptor(policies=[policy])
    interceptor.call("post", _noop, body=AWS_KEY)

    assert seen["body"] == AWS_KEY  # predicate saw the real value
    assert AWS_KEY not in _last_event().args["body"]  # ledger did not


def test_the_secret_detecting_policy_still_blocks_with_redaction_on():
    from chokepoint.policies import no_secrets_in_args

    interceptor = ChokepointInterceptor(policies=[no_secrets_in_args()])
    with pytest.raises(GuardBlocked, match="credential"):
        interceptor.call("post", _noop, body=AWS_KEY)

    # The blocking event itself must not carry the secret it blocked.
    assert AWS_KEY not in str(_last_event().args)


# --- everything the engine writes ------------------------------------------


def test_the_jsonl_sink_never_receives_the_raw_secret(tmp_path):
    sink = tmp_path / "ledger.jsonl"
    ActionLedger.configure(sink_path=sink)

    interceptor = ChokepointInterceptor(policies=[_always_allows()])
    interceptor.call("post", _noop, body=ANTHROPIC_KEY)

    assert ANTHROPIC_KEY not in sink.read_text(encoding="utf-8")


def test_exports_never_contain_the_raw_secret():
    interceptor = ChokepointInterceptor(policies=[_always_allows()])
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
    interceptor = ChokepointInterceptor(policies=[policy])
    with pytest.raises(GuardBlocked):
        interceptor.call("post", _noop, body=AWS_KEY)

    # ...but the args recorded alongside it must still be clean.
    assert AWS_KEY not in str(_last_event().args)


def test_a_raising_tool_does_not_leak_its_arguments():
    def boom(**kwargs):
        raise ValueError(f"bad token {AWS_KEY}")

    interceptor = ChokepointInterceptor(policies=[])
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

    interceptor = ChokepointInterceptor(policies=[policy])
    with pytest.raises(GuardBlocked):
        interceptor.call("post", _noop, body="x")

    assert all(AWS_KEY not in r.reason for r in _last_event().contributing_rules)


# --- escalation messages ---------------------------------------------------


def test_the_escalation_summary_is_redacted():
    """Slack is the least controlled destination Chokepoint writes to."""
    from chokepoint._scope import ExecutionScope
    from chokepoint.core.context import GuardContext
    from chokepoint.decisions import ESCALATE, RuleResult
    from chokepoint.escalation._message import format_escalation_summary

    ctx = GuardContext.build(tool_name="transfer", args={"key": AWS_KEY}, scope=ExecutionScope())
    summary = format_escalation_summary(
        ctx, RuleResult(passed=False, on_fail=ESCALATE, reason="approve", policy_name="p")
    )
    assert AWS_KEY not in summary
    assert "REDACTED" in summary


def test_the_escalation_summary_is_truncated():
    """Slack rejects a message over 40k outright, so a large payload would
    turn every escalation on that tool into a silent delivery failure."""
    from chokepoint._scope import ExecutionScope
    from chokepoint.core.context import GuardContext
    from chokepoint.decisions import ESCALATE, RuleResult
    from chokepoint.escalation._message import MAX_ARGS_CHARS, format_escalation_summary

    ctx = GuardContext.build(tool_name="upload", args={"blob": "x" * 50_000}, scope=ExecutionScope())
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
    out = redact_args({"payload": {"headers": [{"x": f"Bearer {AWS_KEY}"}], "meta": ("a", AWS_KEY)}})
    assert AWS_KEY not in str(out)
    # Container types survive the round trip.
    assert isinstance(out["payload"]["headers"], list)
    assert isinstance(out["payload"]["meta"], tuple)


def test_sets_are_walked_and_stay_sets():
    out = redact_args({"tags": {AWS_KEY, "clean"}})
    assert isinstance(out["tags"], set)
    assert "clean" in out["tags"]
    assert AWS_KEY not in out["tags"]


def test_frozensets_stay_frozen():
    """A redacted hashable is still hashable, so the container type survives."""
    out = redact_args({"tags": frozenset({AWS_KEY})})
    assert isinstance(out["tags"], frozenset)
    assert AWS_KEY not in out["tags"]


def test_bytes_values_are_replaced_wholesale():
    """Bytes have no guaranteed encoding, so replacing only the matching span
    could corrupt everything around it — the whole value goes."""
    out = redact_args({"blob": f"key {AWS_KEY} here".encode()})
    assert out["blob"] == DEFAULT_PLACEHOLDER.encode()


def test_bytearray_values_are_redacted_too():
    out = redact_args({"blob": bytearray(AWS_KEY.encode())})
    assert out["blob"] == DEFAULT_PLACEHOLDER.encode()


def test_clean_bytes_are_left_alone():
    out = redact_args({"blob": b"just a payload"})
    assert out["blob"] == b"just a payload"


def test_dict_keys_are_scrubbed_alongside_values():
    """A mapping keyed by token is unusual but real, and a leaked key leaks
    just as effectively as a leaked value."""
    out = redact_args({"payload": {AWS_KEY: "value"}})
    assert AWS_KEY not in out["payload"]
    assert "REDACTED:aws_access_key_id" in next(iter(out["payload"]))


def test_generators_are_left_untouched():
    """Consuming a one-shot iterable to scrub it would destroy the value."""
    gen = (x for x in ["a"])
    assert redact_args({"stream": gen})["stream"] is gen


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


def test_credit_card_redaction_follows_include_pii_by_default():
    """A card number is PII, so `include_pii=True` turns it on with the rest."""
    configure_redaction(include_pii=True)
    assert "REDACTED:credit_card" in redact_text(f"card {VALID_CARD}")


def test_an_explicit_false_survives_include_pii():
    """It used to be `redact_credit_cards or include_pii`, so an explicit
    `False` alongside `include_pii=True` was silently ignored."""
    configure_redaction(include_pii=True, redact_credit_cards=False)
    assert VALID_CARD in redact_text(f"card {VALID_CARD}")
    # The rest of the PII patterns are still in effect.
    assert "alice@example.com" not in redact_text("alice@example.com")


def test_credit_cards_can_be_redacted_without_the_rest_of_pii():
    configure_redaction(redact_credit_cards=True)
    assert "REDACTED:credit_card" in redact_text(f"card {VALID_CARD}")
    assert redact_text("alice@example.com") == "alice@example.com"


def test_credit_cards_are_off_by_default():
    assert redact_text(f"card {VALID_CARD}") == f"card {VALID_CARD}"


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
            return dict.fromkeys(args, "SCRUBBED")

        def redact_text(self, text):
            return "SCRUBBED"

    configure_redaction(redactor=Upper())
    interceptor = ChokepointInterceptor(policies=[_always_allows()])
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


def test_contains_placeholder_walks_sets():
    assert contains_placeholder({DEFAULT_PLACEHOLDER, "clean"})
    assert contains_placeholder(frozenset({DEFAULT_PLACEHOLDER}))
    assert not contains_placeholder(frozenset({"clean"}))


def test_contains_placeholder_walks_bytes():
    assert contains_placeholder(DEFAULT_PLACEHOLDER.encode())
    assert contains_placeholder(bytearray(b"prefix [REDACTED:jwt]"))
    assert not contains_placeholder(b"clean payload")


def test_contains_placeholder_walks_dict_keys():
    """It has to look everywhere redaction reached, and redaction reaches keys."""
    assert contains_placeholder({DEFAULT_PLACEHOLDER: "clean"})
    assert contains_placeholder({"outer": {"[REDACTED:jwt]": 1}})
    assert not contains_placeholder({"outer": {"inner": 1}})


# --- replay and fixtures acknowledge redaction -----------------------------


def test_replay_flags_a_redacted_event():
    from chokepoint.ledger.ledger import replay

    interceptor = ChokepointInterceptor(policies=[_always_allows()])
    interceptor.call("post", _noop, body=AWS_KEY)

    result = replay(_last_event().event_id)
    assert result.redacted is True


def test_replay_of_a_clean_event_is_not_flagged():
    from chokepoint.ledger.ledger import replay

    interceptor = ChokepointInterceptor(policies=[_always_allows()])
    interceptor.call("post", _noop, body="nothing sensitive")

    assert replay(_last_event().event_id).redacted is False


def test_generated_fixtures_skip_redacted_events():
    """A test replaying placeholders could never pass; dropping it silently
    would overstate coverage instead."""
    from chokepoint.testing.harness import fixtures_from_events

    interceptor = ChokepointInterceptor(policies=[_always_allows()])
    interceptor.call("clean", _noop, body="fine")
    interceptor.call("dirty", _noop, body=AWS_KEY)

    source = fixtures_from_events(ActionLedger.current().events())
    compile(source, "<generated>", "exec")
    assert "pytest.mark.skip" in source
    assert "import pytest" in source
    # The clean event still produces a live test.
    assert source.count("def test_") == 2
    assert source.count("@pytest.mark.skip") == 1


# --- property-based: no credential survives, whatever shape it arrives in ---

#: One real example of every shape in `SECRET_PATTERNS`. Drawing from a fixed
#: list rather than generating credential-like strings keeps the property about
#: redaction rather than about how good the generator is at faking a token.
_SECRET_SAMPLES = [
    AWS_KEY,
    ANTHROPIC_KEY,
    "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "xoxb-1234567890-abcdefghij",
    "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g",
    "-----BEGIN RSA PRIVATE KEY-----",
    "Bearer abcdefghijklmnopqrstuvwxyz0123",
]

#: Filler drawn from a deliberately narrow alphabet: a wide one could generate
#: a credential shape by chance and fail the property for a reason that has
#: nothing to do with the code under test.
_NOISE_TEXT = st.text(alphabet="abcdefghij ", max_size=8)
_HASHABLE_NOISE = st.one_of(_NOISE_TEXT, st.integers(), st.binary(max_size=4), st.none())
_NOISE = st.one_of(_HASHABLE_NOISE, st.lists(_NOISE_TEXT, max_size=3))


@st.composite
def _args_hiding_a_secret(draw):
    """Arguments with a real credential buried at a random depth in a random
    container shape.

    Built by construction rather than by generating structures and hoping a
    secret lands in one: most draws would contain no secret at all and the
    property would pass without testing anything.
    """
    secret = draw(st.sampled_from(_SECRET_SAMPLES))
    shape = draw(st.sampled_from(["bare", "embedded", "bytes"]))
    if shape == "bare":
        value = secret
    elif shape == "embedded":
        value = f"prefix {secret} suffix"
    else:
        value = secret.encode()
    hashable = True

    for wrapper in draw(st.lists(st.sampled_from(["list", "tuple", "dict", "set", "key"]), max_size=3)):
        if wrapper == "list":
            value, hashable = [draw(_NOISE), value], False
        elif wrapper == "tuple":
            value = (value, draw(_HASHABLE_NOISE))
        elif wrapper == "dict":
            value, hashable = {"payload": value, "noise": draw(_NOISE)}, False
        elif wrapper == "set" and hashable:
            value = frozenset({value})
        elif wrapper == "key" and isinstance(value, str):
            value, hashable = {value: draw(_NOISE)}, False
    return {"arg": value, "other": draw(_NOISE)}


def _all_text(value):
    """Every string in a redacted structure, keys included, with bytes decoded."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, bytes | bytearray):
        yield value.decode("utf-8", errors="replace")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _all_text(key)
            yield from _all_text(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _all_text(item)


@given(_args_hiding_a_secret())
@settings(max_examples=200, deadline=None)
def test_no_credential_survives_redaction_at_any_depth(args):
    redacted = redact_args(args)
    for text in _all_text(redacted):
        for label, pattern in SECRET_PATTERNS:
            assert not pattern.search(text), f"{label} survived in {text!r}"


@given(_args_hiding_a_secret())
@settings(max_examples=200, deadline=None)
def test_redaction_never_mutates_the_arguments_it_is_given(args):
    """Policies have already run against these objects, and the caller's own
    tool is about to be handed them — scrubbing has to produce a copy."""
    before = copy.deepcopy(args)
    redact_args(args)
    assert args == before


# --- property-based: the Luhn check ----------------------------------------

_DIGITS = st.text(alphabet="0123456789", min_size=1, max_size=18)


def _luhn_check_digit(payload: str) -> str:
    """The digit that makes `payload` Luhn-valid.

    Derived independently of `_is_luhn_valid` — computing it by asking the
    function under test would make the property vacuous.
    """
    total = 0
    for index, char in enumerate(reversed(payload)):
        value = int(char)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


@given(_DIGITS)
@settings(max_examples=200)
def test_appending_the_check_digit_makes_a_number_luhn_valid(payload):
    assert _is_luhn_valid(payload + _luhn_check_digit(payload))


@given(_DIGITS, st.integers(min_value=1, max_value=9))
@settings(max_examples=200)
def test_a_wrong_check_digit_is_always_detected(payload, offset):
    """Only the check digit is perturbed. Luhn misses some transpositions and
    some compensating pairs, so an arbitrary single-digit edit elsewhere is not
    guaranteed to be caught — a wrong check digit always is.
    """
    correct = int(_luhn_check_digit(payload))
    assert not _is_luhn_valid(payload + str((correct + offset) % 10))


def test_the_card_candidate_scan_is_bounded_on_a_long_digit_run():
    """Cheap insurance: `_CARD_CANDIDATE` has a bounded repetition inside an
    unbounded one, the classic shape for catastrophic backtracking, and it runs
    on every recorded argument once card redaction is enabled.
    """
    redactor = PatternRedactor(redact_credit_cards=True)
    text = "x" + "4" * 5000 + "y"

    start = time.perf_counter()
    redactor.redact_text(text)
    assert time.perf_counter() - start < 1.0
