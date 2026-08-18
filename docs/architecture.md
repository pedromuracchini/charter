# Architecture

This page explains *why* Charter is shaped the way it is. For signature-level
detail see the [API reference](reference/index.md); for how to use each
feature, the [home page](index.md).

## The premise

An agent's authorization rules must not live in the agent's prompt. Anything
the model can read, the model can be argued out of — and an instruction like
"never delete production data" is a suggestion to a text generator, not a
control. Charter expresses those rules as ordinary Python predicates evaluated
by the runtime, outside the model's context. The LLM never sees a policy,
cannot enumerate them, and cannot reason its way past one.

Charter is **middleware, not a harness**. It does not run a plan→act→observe
loop, manage context or memory, or execute tools. It decides whether a tool
call may proceed and what to do when it may not: block it, escalate it to a
human, or let it run and undo it afterwards. It sits between an agent's
tool-calling loop — LangGraph, the OpenAI Agents SDK, MCP, or one you wrote —
and the tools themselves.

## One chokepoint

`evaluate_call()` and its async twin `evaluate_call_async()` are the only
places a guarded call actually runs. `@guard`, `CharterInterceptor.call()` and
`CharterInterceptor.acall()` are thin call-sites: they assemble a
`GuardContext`, gather the applicable policies, and hand off. None of them
duplicates pre/post, escalation, undo, ledger or telemetry logic.

That matters for a security library specifically. A rule enforced in three
places is, eventually, a rule enforced inconsistently in three places. When the
question is "may this call happen?", there should be exactly one answer and
exactly one place that computes it.

Per call, the engine:

1. Builds a `GuardContext` from the tool's arguments and the ambient
   `ExecutionScope`.
2. Records the attempt in `CallState`, before any rule runs, so a rate-limit
   predicate can see the call it is currently deciding on.
3. Evaluates every active policy's pre-hook rules.
4. Resolves the worst failure by precedence, and escalates if needed.
5. Executes the tool — unless blocked, in `enforce` mode.
6. Evaluates post-hook rules, with `ctx.result` populated.
7. Auto-undoes on a post-hook block, when the call wrapped a
   `ReversibleAction`.
8. Records a `LedgerEvent` per hook that reached a decision, and emits an OTEL
   span and metrics alongside it.

## Decisions have precedence, not arrival order

When several rules fail for the same hook, `pick_decision()` ranks them:
**BLOCK > ESCALATE > ALLOW**, then by severity within a level. An
`on_fail=ALLOW` rule is a log-only rule — recorded when it fires, never
enforced, even if it fires beside a BLOCK.

The winner drives the outcome, but the ledger records *all* of them, in
`LedgerEvent.contributing_rules`. Recording only the winner meant an audit
could not ask "what else was wrong with this call?" — three simultaneous
failures looked identical to one.

Every `RuleResult` also carries the `policy_hash` of the policy that produced
it, so a decision can be traced back to the exact policy version that made it.

## Only `enforce` mode may have side effects

`dry_run` and `observe` exist to answer "what would these policies do?" against
real traffic. That is worthless if asking the question pages a human, so
outside `enforce` mode the engine resolves an ESCALATE, records it and spans it
— but never contacts the handler. Nothing is posted to Slack, no approval
webhook fires, nothing blocks on `input()`. The recorded reason is suffixed so
an audit can tell "denied" from "never asked". By the same rule, non-enforce
modes never raise `GuardBlocked` and never auto-undo.

## Nothing in your code may crash the call it guards

A bug in a policy predicate, an undo function or an escalation handler must not
take down the tool call, and must not vanish from the audit trail.

- **Predicates fail closed.** An exception inside a predicate becomes a
  synthetic failing `RuleResult` with `on_fail=BLOCK` and `severity="high"`,
  *regardless of what the rule declared* — a broken predicate cannot be trusted
  to honor its own ESCALATE or log-only semantics. `active_when`/`applies_to`
  fail the other way: an exception there is logged and the policy is treated as
  **active**, never silently skipped.
- **Escalation timeouts are enforced by the engine**, not left to the handler.
  A sync handler runs in a disposable thread pool; an async one is awaited with
  `asyncio.wait_for`. Either way a timeout or an exception denies. A handler
  that is `async def` but reached through the sync engine is detected and
  denied, because `bool(coroutine)` is always `True` and would approve
  everything.
- **Undo failures do not erase the ledger event.** An exception from `undo()`
  during a post-block is caught, folded into the recorded reason, and the event
  is still written and `GuardBlocked` still raised. Losing the audit trail at
  the exact moment an undo failed would defeat the point of having one.
- **Audit-sink failures do not fail the call.** Recording happens after the
  tool has already executed; a full disk must not turn a call the policies
  allowed into an exception. Sink write failures are logged and counted in
  `ActionLedger.sink_error_count`.

## Async is detected, not a parallel API

Predicates are always plain sync callables — cheap, deterministic checks, not
I/O. Only tool invocation, `ReversibleAction.do_fn`/`undo_fn` and
`EscalationHandler.escalate` may be async, and each is detected with
`inspect.iscoroutinefunction`. There is no `@guard_async` and no separate async
policy class: `@guard` returns an async wrapper for an async tool, and
`interceptor.acall()` is the async sibling of `.call()`.

## Identity is ambient, not a parameter

`@guard`-decorated functions keep their normal signature — no `ctx` parameter —
so caller identity has to arrive some other way. `ExecutionScope`, propagated
through a `contextvars.ContextVar`, carries session, role, trust level and
delegation chain. The interceptor installs a fresh scope per call, so a
`@guard`-decorated helper invoked *from inside* an intercepted tool sees the
same caller. Outside any interceptor, `charter.session(...)` sets it
explicitly.

Tool arguments and interceptor options live in separate namespaces. `call()`
takes the tool's arguments as `**kwargs` for brevity, but a tool that declares
an argument named `session_id` or `domain` must pass `args={...}` instead —
and Charter warns when it detects the collision rather than silently dropping
the value.

## `Policy` is one interface over very different rule shapes

`PolicySet` (ANDed rules), `AgentScopedPolicy` (a role-gated single rule) and
the composites built by `&`, `|` and `~` all implement the same three-member
interface: `is_active()`, `evaluate()`, `policy_hash`. The engine, the linter
and the reports only ever talk to that interface, so a new policy shape needs
no changes anywhere else.

Composite semantics are a pragmatic choice — internally consistent rather than
forced by logic:

| Operator | Active when | Result |
|---|---|---|
| `a & b` | either child is active | both children's results concatenated; failing any rule fails the AND |
| `a \| b` | either child is active | passes if at least one active child fully passes, otherwise reports every active child's failures |
| `~a` | the child is active | fails when the child raised *no* violation; passes when it had at least one |

## Irreversibility is enforced as an intrinsic rule

`ReversibleAction.intrinsic_check()` returns a synthetic, always-firing rule:
an unconditional BLOCK for `"permanent"`, an unconditional ESCALATE for
`"high"`, nothing for `"low"` and `"medium"`. The engine prepends it to the
pre-hook rule list, so irreversibility gets exactly the same evaluation, ledger
and span treatment as any hand-written policy. There is no second enforcement
path to keep in sync.

`"medium"` without an `undo_fn` raises at construction, not at call time.
`"high"` without a resolvable `escalate_to` warns at construction, because it
would otherwise resolve to the fail-safe denier and be indistinguishable from
`"permanent"`.

## Escalation: pluggable, safe by default

An `escalate_to="scheme://..."` URI's scheme selects a handler registered with
`register_handler()`. With nothing registered, `FailSafeEscalationHandler` logs
and **denies** — an escalation that cannot be resolved must never be treated as
an approval.

The ledger decision stays `"ESCALATE"` regardless of outcome; whether it was
approved is folded into the reason text, while the engine separately tracks
whether the call may proceed. Real handlers — Slack, webhook, CLI — ship in
`charter.escalation` and are deliberately *not* auto-registered: there is no
way to detect that an agent wants Slack.

## Redaction happens at record time

The ordering is the whole design:

> evaluate against real values → redact → write

Policies must see the true arguments; a predicate written to check a credential
cannot check a placeholder. So redaction runs inside the engine's recording
step, after every rule has run and at the last moment before the values become
durable. `ctx.args` is never rewritten.

Five destinations are covered: the in-memory ledger, the JSONL sink, the
JSON/CSV exports, and the escalation message — the least controlled of them,
since it lands in a channel with its own membership. Free-text fields are
scrubbed too, because a fail-closed predicate folds its exception text into the
reason, and exception text routinely quotes the argument that broke it.

Secrets are scrubbed by default; PII is opt-in. The secret patterns are
anchored on literal markers (`sk-ant-`, `AKIA`, `-----BEGIN`), so false
positives are rare. PII patterns are far likelier to match something a policy
legitimately reasons about — an email address is often the point of the call.

The honest tradeoff: `replay()` rebuilds its context from the stored event, so
replaying a redacted call evaluates policies against placeholders.
`ReplayResult.redacted` flags it, and fixture generation emits a skip marker
rather than a test that cannot pass.

## Cross-call state lives beside the context, not inside it

A `GuardContext` is a value describing one call, which leaves no room for
"block after N calls" or "stop once this session has spent $X". `CallState`
adds that memory as a separate, lock-guarded, LRU-bounded object injected into
the scope and read through `ctx.calls_this_session()` and `ctx.spent()`.

Two consequences are deliberate. `GuardContext` stays a value — it holds a
reference, not history. And replay stays deterministic: a replayed call gets no
`CallState`, so a history-dependent policy reads zero rather than silently
consulting today's live counters.

Counting is on **attempts**: a blocked call still counts, or retrying a denial
would be free.

## A budget's shape follows when its cost becomes knowable

This is not a style choice:

- **`amount_from`** — the cost is in the arguments (a transfer amount). The
  pre-hook rejects any call that *would* breach the cap, so it is **never
  exceeded**.
- **`actual_from`** — the cost is only in the response (LLM token usage). The
  pre-hook can only ask whether the budget is *already* gone, so the semantics
  are **stop once spent**: the call that crosses the line completes, and the
  next one is refused.

That one-call overshoot is inherent to not knowing a price before paying it.
Passing both bounds it — an estimate is charged at the check and superseded by
the real figure at the post-hook.

No pricing table ships with the library. Rates change, vary by tier and region,
and a stale constant inside a security library would silently mis-bill.

Check and charge are separate hooks, so concurrent calls *within one session*
can each pass the check before any charges land. A budget here is a quota, not
a hard financial control.

## Multi-agent: identity on the interceptor, never on the tool

Delegation between agents can silently escalate privilege: agent A delegates to
agent B, B has access A doesn't, and nothing ever explicitly authorized that —
the confused-deputy problem. A collection of individually safe agents is not a
collectively safe system.

Charter's answer is to share the tool but never share an interceptor without
identity. The same tool function passed to two interceptors can be BLOCK for
one agent and ALLOW for another, because `ctx.caller_role` differs. For many or
dynamic agents, one shared `CharterRegistry` replaces per-interceptor wiring.

`delegation_chain` has one convention: **register ancestors, read the full
path.** The registry takes the agent's ancestors; the interceptor appends the
acting agent when building the scope, so every context, ledger event and graph
sees the complete lineage. `delegation_depth()` counts hops, not names.

An interceptor with no `agent_id` leaves `ctx.caller_role` permanently `None`,
silently defeating any role-scoped policy. `charter lint` reports that as an
error.

## Sampling is rolled once

Sampling is decided once per event and the result is passed to the span
emitter, so the ledger and the traces never disagree about which events
survived. A failing rule — BLOCK, ESCALATE, or a log-only ALLOW — is *always*
recorded to the ledger; only its span is sampled. When every applicable rule
passes, one aggregate ALLOW entry is recorded, and that single roll governs
both the entry and its span.

A tool that raises is recorded unconditionally and never sampled: the call was
authorized, ran and failed, and an audit trail that omits that is lying by
omission. No span is emitted for it, because Charter decided nothing there —
whatever instruments the tool owns that part of the trace.

## Deliberately out of scope

Some things are missing on purpose rather than by omission:

- **Live OTEL gauges** for block rate and coverage — these windowed statistics
  are computed on demand from the ledger instead of pushed as continuously
  updating instruments.
- **General logical-conflict detection between policies** — the linter catches
  dead policies, duplicate names and the scoped-policy-without-registry
  anti-pattern, but does not try to prove two arbitrary predicates contradict
  each other.
- **Automatic test-input synthesis** — fixtures are harvested from real
  recorded decisions, not synthesized for arbitrary lambdas, which would need
  constraint-solving over arbitrary Python.
- **Policy hot-reload, rollback and anomaly-rate alerting** — production
  operations features, not library features.
- **PDF export** — JSON, CSV, DOT, Mermaid and a natural-language narrative
  cover the "get this data out in a reviewable form" need without a rendering
  dependency.

## What Charter is not

The shipped policy matchers — destructive SQL and shell, path confinement,
domain allowlists — are **seatbelts against agent mistakes and opportunistic
prompt injection, not a sandbox.** An adversary in full control of the input
can evade a pattern matcher. If your threat model includes that adversary, the
answer is a real sandbox with a real kernel boundary; Charter is the
authorization and reversibility layer that sits above it.
