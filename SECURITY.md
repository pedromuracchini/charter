# Security Policy

## Reporting a vulnerability

Please do **not** open a public GitHub issue for a suspected security
vulnerability. Instead, email **bonadioar@gmail.com** with:

- A description of the vulnerability and its potential impact.
- Steps to reproduce (a minimal policy/tool-call snippet is usually enough).
- The version of `charter` affected (`python -c "import charter; print(charter.__version__)"`).

We aim to acknowledge reports within a few business days.

## Trust boundaries to be aware of

Charter is an authorization layer, not a sandbox. A few things worth being
explicit about:

- **The CLI executes arbitrary code.** `charter report`, `charter lint`,
  `charter replay --agent <file>`, and `charter repl --agent <file>` all
  load the target file as a Python module via `importlib.util` and execute
  it (`exec_module`) to read its module-level `POLICIES`/`REGISTRY`. This is
  inherent to how the CLI discovers policies — **only ever point it at files
  you trust**, the same way you wouldn't run `python untrusted_file.py`.
- **Policy predicates run with the same privileges as your agent process.**
  `pre`/`post` lambdas, `active_when`, and `applies_to` are plain Python
  callables evaluated in-process — they are not sandboxed. Charter protects
  against *bugs* in predicates (a raising predicate fails closed to `BLOCK`
  rather than crashing the call — see `CLAUDE.md`), not against *malicious*
  predicate code.
- **The default `EscalationHandler` denies by design.** With no handler
  registered for an `escalate_to` scheme, escalations are logged and denied
  (fail-safe) rather than silently approved. Three real handlers ship under
  `charter.escalation`:
  - `SlackEscalationHandler` **requires** a non-empty `approvers` set (Slack
    user IDs) at construction — without it, *anyone* in the channel reacting
    with the approve emoji would approve the action. Keep your bot token out
    of source control (an env var, a secrets manager — not a literal in code).
  - `WebhookEscalationHandler` trusts whatever `url` you configure and
    whatever JSON body it returns — put your own authentication in `headers`
    (a shared secret, a bearer token) and only ever point it at an endpoint
    you control.
  - `CLIEscalationHandler` is local-only (stdin), no network exposure, but
    only meaningful when someone is actually watching the terminal.
  If you implement your own `EscalationHandler` instead, the same rule
  applies: make sure it authenticates its approval source — Charter has no
  opinion on how you verify "a human approved this."
- **Tool arguments are redacted before they are recorded, but redaction is
  best-effort.** Values matching a known credential shape, and values under
  names like `password`/`api_key`/`authorization`, are replaced before they
  reach the ledger, the JSONL sink, the compliance exports or an escalation
  message (see `charter.redaction`). PII patterns are opt-in via
  `configure_redaction(include_pii=True)`. This is pattern matching, not
  classification: a credential in a shape Charter doesn't recognise, or an
  identifier under a name it doesn't know, is recorded verbatim. Add your own
  with `keys=` / `extra_patterns=`, or plug in a real DLP scrubber via
  `configure_redaction(redactor=...)`.
- **`ActionLedger`'s JSONL sink (`sink_path`) is plain-text and unencrypted.**
  Redaction reduces what ends up there; it does not make the file safe to
  leave world-readable. Store it somewhere access-controlled, and treat it as
  sensitive regardless — tool names, caller identities, session ids and
  argument *structure* are all still present.
- **Policies see unredacted arguments, by design.** A predicate written to
  check a credential has to be able to see one. Redaction applies to what is
  written, not to what is evaluated — so a malicious or buggy predicate can
  still observe (and, if it does I/O, exfiltrate) raw argument values. This is
  the same trust boundary as "predicates are unsandboxed", above.

## Supported versions

Pre-`1.0.0`, only the latest released version receives fixes.
