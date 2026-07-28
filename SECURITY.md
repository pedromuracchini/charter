# Security Policy

## Reporting a vulnerability

Please do **not** open a public GitHub issue for a suspected security
vulnerability. Instead, email **bonadioar@gmail.com** with:

- A description of the vulnerability and its potential impact.
- Steps to reproduce (a minimal policy/tool-call snippet is usually enough).
- The version of `tollgate` affected (`python -c "import tollgate; print(tollgate.__version__)"`).

We aim to acknowledge reports within a few business days.

## Trust boundaries to be aware of

Tollgate is an authorization layer, not a sandbox. A few things worth being
explicit about:

- **The CLI executes arbitrary code.** `tollgate report`, `tollgate lint`,
  `tollgate replay --agent <file>`, and `tollgate repl --agent <file>` all
  load the target file as a Python module via `importlib.util` and execute
  it (`exec_module`) to read its module-level `POLICIES`/`REGISTRY`. This is
  inherent to how the CLI discovers policies — **only ever point it at files
  you trust**, the same way you wouldn't run `python untrusted_file.py`.
- **Policy predicates run with the same privileges as your agent process.**
  `pre`/`post` lambdas, `active_when`, and `applies_to` are plain Python
  callables evaluated in-process — they are not sandboxed. Tollgate protects
  against *bugs* in predicates (a raising predicate fails closed to `BLOCK`
  rather than crashing the call — see `CLAUDE.md`), not against *malicious*
  predicate code.
- **The default `EscalationHandler` denies by design.** With no handler
  registered for an `escalate_to` scheme, escalations are logged and denied
  (fail-safe) rather than silently approved. Three real handlers ship under
  `tollgate.escalation`:
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
  applies: make sure it authenticates its approval source — Tollgate has no
  opinion on how you verify "a human approved this."
- **`ActionLedger`'s JSONL sink (`sink_path`) is plain-text and unencrypted**,
  and may contain tool call arguments (`ctx.args`) verbatim. Don't pass
  secrets as tool arguments if the ledger sink isn't stored somewhere
  access-controlled.

## Supported versions

Pre-`1.0.0`, only the latest released version receives fixes.
