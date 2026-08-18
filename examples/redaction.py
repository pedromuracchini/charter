"""Keeping secrets and PII out of the audit trail.

Charter records `ctx.args` for every decision, and those records reach the
in-memory ledger, the JSONL file on disk, the JSON/CSV exports, and the
escalation message posted into a Slack channel. Redaction scrubs all of them
at once.

The ordering is the important part: **policies evaluate against the real
arguments, and only what gets written is scrubbed.** A policy that inspects a
credential has to be able to see it.

Run directly:

    uv run python examples/redaction.py
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import charter
from charter import BLOCK, CharterInterceptor, GuardBlocked, PolicySet
from charter.policies import no_secrets_in_args

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
SINK = Path(tempfile.mkdtemp(prefix="charter-redaction-")) / "ledger.jsonl"


def send_email(to: str, body: str, mrn: str | None = None) -> dict:
    return {"sent": to}


def call_api(url: str, authorization: str, payload: dict) -> dict:
    return {"status": 200}


def allow_everything() -> PolicySet:
    policy = PolicySet("audit_only")
    policy.require(lambda ctx: True, on_fail=BLOCK, reason="recorded for audit")
    return policy


def show(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    charter.configure_ledger(sink_path=SINK)
    interceptor = CharterInterceptor(policies=[allow_everything()], agent_id="ops_agent")

    show("default: secrets scrubbed, everything else intact")
    interceptor.call(
        "call_api",
        call_api,
        url="https://api.example.com/v1",
        authorization=f"Bearer {AWS_KEY}",
        payload={
            "token": "an ordinary looking value",
            "notes": ["deploy key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345", "all good"],
        },
    )
    event = charter.ActionLedger.current().events()[-1]
    print(f"  url             {event.args['url']}")
    print(f"  authorization   {event.args['authorization']}")
    print(f"  payload.token   {event.args['payload']['token']}")
    print(f"  payload.notes   {event.args['payload']['notes']}")
    print("  ^ `authorization` and the nested `token` matched sensitive *key names*, so the")
    print("    whole value went — no pattern would have caught 'an ordinary looking value'.")
    print("    Inside `notes` a *pattern* matched, so only that span was replaced and the")
    print("    surrounding text stayed readable. `url` was left entirely alone.")

    show("PII is opt-in — an email is often the point of the call")
    interceptor.call("send_email", send_email, to="alice@example.com", body="hi")
    print(f"  to  {charter.ActionLedger.current().events()[-1].args['to']}   (recorded as-is)")

    charter.configure_redaction(include_pii=True, keys=["mrn", "dob"])
    interceptor.call("send_email", send_email, to="alice@example.com", body="hi")
    print(f"  to  {charter.ActionLedger.current().events()[-1].args['to']}   (after opting in)")

    show("a domain-specific field, redacted by name")
    interceptor.call("send_email", send_email, to="x@y.com", body="mrn on file", mrn="MRN-99887")
    print(f"  mrn {charter.ActionLedger.current().events()[-1].args['mrn']}")

    show("your own pattern")
    charter.configure_redaction(extra_patterns=[("employee_id", re.compile(r"\bEMP-\d{5}\b"))])
    interceptor.call("send_email", send_email, to="x@y.com", body="approved by EMP-12345")
    print(f"  body {charter.ActionLedger.current().events()[-1].args['body']}")

    show("policies still see the real values")
    charter.configure_redaction()  # back to defaults
    guarded = CharterInterceptor(policies=[no_secrets_in_args()], agent_id="ops_agent")
    try:
        guarded.call("call_api", call_api, url="https://x", authorization=AWS_KEY, payload={})
    except GuardBlocked as exc:
        print(f"  blocked: {exc.decision.reason}")
    blocked = charter.ActionLedger.current().events()[-1]
    print(f"  ...and the blocking record itself is clean: {blocked.args['authorization']}")

    show("nothing raw ever reached the disk")
    raw = SINK.read_text(encoding="utf-8")
    print(f"  {SINK}")
    print(f"  {len(raw.splitlines())} events written")
    print(f"  contains the AWS key?  {AWS_KEY in raw}")
    print(f"  contains 'REDACTED'?   {'REDACTED' in raw}")

    show("the tradeoff: replay of a redacted event is not comparable")
    result = charter.replay(blocked.event_id)
    print(f"  ReplayResult.redacted = {result.redacted}")
    print("  Predicates would see placeholders, not the values they originally judged.")
    print("  `charter export --format fixtures` skips these rather than emitting")
    print("  tests that cannot pass.")


if __name__ == "__main__":
    main()
