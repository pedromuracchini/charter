"""Redaction of secrets and PII on the way into the audit trail.

Tollgate records `ctx.args` verbatim for every decision, and those records go
to more places than people expect: the in-memory ledger, the JSONL sink on
disk, the JSON/CSV compliance exports, and the escalation message posted into
a Slack channel. An agent that passes an API key or a patient identifier to a
guarded tool leaks it into all of them at once. That is a poor trade for a
library whose entire job is authorization and auditability.

**Redaction happens at record time, never at evaluation time.** Policies
receive the real `ctx.args` — they have to, or a predicate could not check the
value it was written to check. Only what gets *persisted* is scrubbed. The
sequence is: evaluate against real values, then redact, then write.

**Secret patterns are on by default; PII patterns are not.** A credential in
the ledger has no upside, and the secret patterns are anchored on literal
markers (`sk-ant-`, `AKIA`, `-----BEGIN`), so false positives are rare enough
that a safe default costs nothing. PII patterns are broader and much likelier
to match something a policy genuinely reasons about — an email address is
often the point of the call — so enabling them is a deliberate choice:

    tollgate.configure_redaction(include_pii=True, keys=["ssn", "dob"])

Turn it off entirely with `configure_redaction(enabled=False)`.

One consequence worth knowing: `tollgate.replay()` reconstructs its context
from the stored event, so replaying a call whose arguments were redacted feeds
placeholders to the predicates. `ReplayResult.redacted` flags this, and
`fixtures_from_events` skips those events rather than emitting tests that
cannot pass.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any, Protocol, runtime_checkable

#: What a redacted value is replaced with. Deliberately not empty and not the
#: original length — a placeholder that preserved length would leak it.
DEFAULT_PLACEHOLDER = "[REDACTED]"

PatternSpec = tuple[str, "re.Pattern[str]"]


def _labelled(placeholder: str, label: str) -> str:
    """`[REDACTED:aws_access_key_id]` — says what was removed without saying
    what it was, which is usually all a debugger needs."""
    return f"{placeholder[:-1]}:{label}]" if placeholder.endswith("]") else f"{placeholder}:{label}"


#: High-confidence credential shapes. Every one is anchored on a literal
#: marker rather than an entropy heuristic, which is what makes them safe to
#: run by default: ordinary prose does not contain `-----BEGIN PRIVATE KEY-----`.
SECRET_PATTERNS: tuple[PatternSpec, ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9_\-.=]{20,}")),
)

#: Personal data. Broader than the secret patterns and much likelier to match
#: something the agent is legitimately working with, so opt-in via
#: `configure_redaction(include_pii=True)`.
PII_PATTERNS: tuple[PatternSpec, ...] = (
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("us_ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    ("ip_address", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # Formatted numbers only — a bare run of digits is far too often an id,
    # an amount or a timestamp.
    ("phone", re.compile(r"\+\d{1,3}[\s.\-]?\(?\d{2,4}\)?[\s.\-]?\d{3,4}[\s.\-]?\d{3,4}\b")),
)

#: Candidate card numbers, Luhn-checked before redaction — see `_is_luhn_valid`.
_CARD_CANDIDATE = re.compile(r"\b(?:\d[ \-]?){13,19}\b")

#: Argument names whose value is replaced wholesale, whatever it looks like.
#: A field called `password` is a secret even when its value looks like prose.
DEFAULT_SENSITIVE_KEYS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "private_key",
    "client_secret",
    "authorization",
    "credential",
    "credentials",
)


def _is_luhn_valid(digits: str) -> bool:
    """Luhn checksum. Without it, `redact_credit_cards` would eat any 16-digit
    order number; with it, false positives are ~1 in 10."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


@runtime_checkable
class Redactor(Protocol):
    """Anything that can scrub arguments and free text before they are stored.

    A `Protocol`, not an ABC: a caller with an existing scrubber (a corporate
    DLP client, say) can satisfy it without inheriting from Tollgate.
    """

    def redact_args(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Return a scrubbed copy of a tool's arguments. Must not mutate."""
        ...

    def redact_text(self, text: str) -> str:
        """Scrub free text — a reason string, an exception message."""
        ...


class NullRedactor:
    """Records everything verbatim. Installed by `configure_redaction(enabled=False)`."""

    def __repr__(self) -> str:
        return "<NullRedactor>"

    def redact_args(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Return `args` unchanged, as a shallow copy."""
        return dict(args)

    def redact_text(self, text: str) -> str:
        """Return `text` unchanged."""
        return text


class PatternRedactor:
    """The default redactor: sensitive keys by name, secrets by pattern.

    Two independent mechanisms, because they catch different things:

    - **By key** — an argument named `password` is replaced wholesale. Its
      value may be an ordinary-looking string that no pattern would match.
    - **By pattern** — a credential embedded anywhere inside a value, however
      deeply nested, with only the matching span replaced so the surrounding
      text stays readable.

    Nested containers are walked — dicts, lists, tuples, sets and frozensets —
    because a credential in a JSON payload is the common case, not an edge
    one. Dict *keys* are scrubbed alongside values: a mapping keyed by token is
    unusual but real, and a leaked key leaks just as effectively as a value.
    `bytes` values are checked too, and replaced wholesale when they match.

    Generators and other one-shot iterables are left untouched: consuming one
    to scrub it would destroy the value being recorded.
    """

    def __init__(
        self,
        patterns: Iterable[PatternSpec] = SECRET_PATTERNS,
        keys: Iterable[str] = DEFAULT_SENSITIVE_KEYS,
        *,
        placeholder: str = DEFAULT_PLACEHOLDER,
        redact_credit_cards: bool = False,
    ) -> None:
        self.patterns = tuple(patterns)
        # Lower-cased for case-insensitive matching against argument names.
        self.keys = frozenset(k.lower() for k in keys)
        self.placeholder = placeholder
        self.redact_credit_cards = redact_credit_cards

    def __repr__(self) -> str:
        labels = ",".join(label for label, _ in self.patterns)
        return (
            f"<PatternRedactor patterns={len(self.patterns)}[{labels}] "
            f"keys={len(self.keys)} credit_cards={self.redact_credit_cards}>"
        )

    def redact_text(self, text: str) -> str:
        for label, pattern in self.patterns:
            text = pattern.sub(_labelled(self.placeholder, label), text)
        if self.redact_credit_cards:
            text = _CARD_CANDIDATE.sub(self._maybe_card, text)
        return text

    def _maybe_card(self, match: re.Match[str]) -> str:
        digits = re.sub(r"[ \-]", "", match.group())
        if len(digits) < 13 or not _is_luhn_valid(digits):
            return match.group()
        return _labelled(self.placeholder, "credit_card")

    def _is_sensitive_key(self, key: object) -> bool:
        return isinstance(key, str) and key.lower() in self.keys

    def _redact_bytes(self, value: bytes | bytearray) -> Any:
        """Replace a byte string wholesale when it contains a secret.

        Text has only its matching span replaced, which keeps the surroundings
        readable. Bytes have no guaranteed encoding, so locating the span by
        decoding and then re-encoding could corrupt everything around it —
        replacing the whole value is the conservative choice.
        """
        decoded = value.decode("utf-8", errors="replace")
        return value if self.redact_text(decoded) == decoded else self.placeholder.encode()

    def _redact_key(self, key: Any) -> Any:
        """Scrub a mapping key the same way its values are scrubbed.

        Routed through `_redact_value` rather than special-casing `str`, so a
        `bytes` key holding a credential is caught too — it was not, while the
        identical value one position to the right was, and
        `contains_placeholder` walked bytes keys either way, so such a leak was
        both unredacted and undetectable. Redacting a hashable yields a
        hashable, so the result is still a legal key.
        """
        return self._redact_value(key)

    def _redact_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, bytes | bytearray):
            return self._redact_bytes(value)
        if isinstance(value, Mapping):
            return {
                self._redact_key(key): (
                    self.placeholder if self._is_sensitive_key(key) else self._redact_value(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, AbstractSet):
            # Redacting a hashable yields a hashable, so the result is still a
            # legal set member.
            redacted_items = {self._redact_value(item) for item in value}
            return frozenset(redacted_items) if isinstance(value, frozenset) else redacted_items
        # `str`/`bytes` are Sequences too, and already handled above.
        if isinstance(value, Sequence) and not isinstance(value, str | bytes):
            redacted = [self._redact_value(item) for item in value]
            return tuple(redacted) if isinstance(value, tuple) else redacted
        return value

    def redact_args(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Return a scrubbed copy of a tool's arguments. Never mutates `args`."""
        return {
            key: self.placeholder if self._is_sensitive_key(key) else self._redact_value(value)
            for key, value in args.items()
        }


#: Secrets are scrubbed out of the box — see the module docstring for why this
#: default is on while PII is not.
_DEFAULT_REDACTOR: Redactor = PatternRedactor()

_redactor: Redactor = _DEFAULT_REDACTOR
_lock = threading.Lock()


def configure_redaction(
    *,
    enabled: bool = True,
    keys: Iterable[str] | None = None,
    include_pii: bool = False,
    redact_credit_cards: bool | None = None,
    extra_patterns: Iterable[PatternSpec] = (),
    placeholder: str = DEFAULT_PLACEHOLDER,
    redactor: Redactor | None = None,
) -> Redactor:
    """Install the process-wide redactor, and return it.

    Call once at startup, before the first guarded call — events already
    recorded are not revisited.

        tollgate.configure_redaction(include_pii=True, keys=["ssn", "mrn"])

    `keys` *replaces* `DEFAULT_SENSITIVE_KEYS`; pass
    `keys=[*DEFAULT_SENSITIVE_KEYS, "mrn"]` to extend it instead.
    `extra_patterns` always adds to whatever pattern set is in effect.
    `redactor` overrides everything with your own implementation, for wiring
    up an existing DLP service.

    `redact_credit_cards` defaults to following `include_pii`, since a card
    number is PII; pass it explicitly to decouple the two. It used to be
    `redact_credit_cards or include_pii`, which meant an explicit `False`
    alongside `include_pii=True` was silently ignored.
    """
    global _redactor
    if redactor is None:
        if not enabled:
            redactor = NullRedactor()
        else:
            patterns = [*SECRET_PATTERNS]
            if include_pii:
                patterns.extend(PII_PATTERNS)
            patterns.extend(extra_patterns)
            redactor = PatternRedactor(
                patterns=patterns,
                keys=DEFAULT_SENSITIVE_KEYS if keys is None else keys,
                placeholder=placeholder,
                redact_credit_cards=(include_pii if redact_credit_cards is None else redact_credit_cards),
            )
    with _lock:
        _redactor = redactor
    return redactor


def current_redactor() -> Redactor:
    """The redactor every record-time call site consults.

    Read without taking `_lock`: rebinding a module global is atomic, so a
    caller sees either the old redactor or the new one, never a torn value.
    Locking here would put a mutex acquisition on the path of every single
    recorded event to protect one pointer read. Writers still lock, so two
    concurrent `configure_redaction()` calls can't interleave.
    """
    return _redactor


def reset_redaction() -> None:
    """Restore the default (secrets on, PII off). Intended for tests."""
    global _redactor
    with _lock:
        _redactor = _DEFAULT_REDACTOR


def redact_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Scrub a tool's arguments with the configured redactor."""
    return current_redactor().redact_args(args)


def redact_text(text: str) -> str:
    """Scrub free text with the configured redactor."""
    return current_redactor().redact_text(text)


def contains_placeholder(value: Any, placeholder: str = DEFAULT_PLACEHOLDER) -> bool:
    """Whether anything in `value` was redacted.

    Used by `replay()` to warn that a reconstructed context holds placeholders
    rather than the values the policies originally saw. Walks the same shapes
    `PatternRedactor` scrubs, keys included — a report that a value survived
    redaction has to look everywhere redaction reached.
    """
    marker = placeholder.rstrip("]")
    if isinstance(value, str):
        return marker in value
    if isinstance(value, bytes | bytearray):
        return marker.encode() in value
    if isinstance(value, Mapping):
        return any(
            contains_placeholder(key, placeholder) or contains_placeholder(item, placeholder)
            for key, item in value.items()
        )
    if isinstance(value, AbstractSet | Sequence) and not isinstance(value, str | bytes):
        return any(contains_placeholder(item, placeholder) for item in value)
    return False
