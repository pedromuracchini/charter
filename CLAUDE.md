# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Tollgate is a Python framework for expressing AI agent authorization policies as
deterministic, code-defined predicates evaluated by the runtime, never by the
LLM itself. Every guarded tool call is intercepted before execution (pre-hook),
and optionally after execution (post-hook), against a set of `Policy` objects.
The core principle: an LLM never sees, interprets, or reasons its way around a
policy — policies are plain Python, evaluated entirely outside the model's
context, by a deterministic runtime "tollgate" every tool call passes through.

Positioning: Tollgate is the authorization-and-reversibility layer of an agent
harness, not a full harness. It doesn't run a plan→act→observe loop, manage
context/memory, or execute tools itself — it decides *whether* a tool call may
proceed and *what to do* if it can't (block, escalate, or auto-undo). It's
framework-agnostic middleware, meant to sit between an agent's tool-calling
loop (LangGraph, CrewAI, a hand-rolled loop, ...) and the tools themselves.

The package is published as `tollgate` (`pip install tollgate`).

## Commands

This project uses **uv**, not raw pip/venv.

```bash
uv sync --extra otel          # install the project + dev + otel deps into .venv
uv run pytest -q               # run the full test suite (sync + async, via pytest-asyncio)
uv run pytest --cov=tollgate --cov-report=term-missing   # with coverage
uv run pytest tests/core/test_reversible.py::test_permanent_blocks -q  # single test
uv run ruff check .            # lint (line-length=110, py311 target)
uv run ruff check --fix .      # autofix
uv run mypy src/tollgate       # strict type check
uv build                       # build sdist + wheel
uv run python examples/clinical.py        # run the worked example
uv run python examples/async_tool.py      # run the async worked example
uv run python examples/real_escalation_handlers.py   # Slack/webhook/CLI escalation handlers
uv run tollgate report --agent examples/clinical.py             # CLI: policy report
uv run tollgate report --agent examples/clinical.py --format mermaid
uv run tollgate lint examples/clinical.py
uv run tollgate replay <event_id>
uv run tollgate repl --agent examples/clinical.py
```

## Architecture

### The evaluation engine is the single chokepoint — with a sync and an async twin

`src/tollgate/_engine.py:evaluate_call()` (and its async sibling
`evaluate_call_async()`) is the one place every guarded tool call actually
runs through. `@guard` (`core/decorator.py`), `TollgateInterceptor.call()`,
and `TollgateInterceptor.acall()` (`core/interceptor.py`) are thin call-sites
that build a `GuardContext`, gather the right `Policy` list, and hand off to
one of these two functions — they do not duplicate any
pre/post/escalate/undo/ledger/OTEL logic themselves. When changing how a
decision is made, recorded, or enforced, `_engine.py` is almost always the
file to edit (usually in **both** `evaluate_call`/`evaluate_call_async` — they
intentionally mirror each other line-for-line where possible), not the
call-sites.

Pipeline per call: build `GuardContext` → evaluate pre-hook rules across all
active policies → resolve the worst failure (BLOCK > ESCALATE > ALLOW
precedence, see `decisions.pick_decision`) → if ESCALATE **and** the mode is
`enforce`, call the resolved `EscalationHandler` (with `timeout_s` actually
enforced — see below) →
execute the tool (unless blocked in `enforce` mode) → evaluate post-hook rules
→ on a post-BLOCK, auto-invoke `ReversibleAction.undo` if the call wrapped one
(a failure here still records the ledger event — see below) → record one
`LedgerEvent` per hook that had a qualifying decision (always for
BLOCK/ESCALATE/log-only-ALLOW; sampled for an aggregate "everything passed"
ALLOW) → emit a `tollgate.evaluate` OTEL span and metrics alongside it.

### Only `enforce` mode may have side effects beyond recording

`dry_run` and `observe` exist to answer "what would these policies do?" against
real traffic. That is worthless if asking the question pages a human, so
`_engine._unresolved_escalation` short-circuits every ESCALATE outside
`enforce`: the decision is still resolved to `ESCALATE`, still recorded to the
ledger, still spanned — but no `EscalationHandler` is contacted, so nothing is
posted to Slack, no approval webhook fires, and nothing blocks on `input()`.
The reason text is suffixed so the ledger distinguishes "denied" from "never
asked". Same rule for the other two side-effecting steps, which were already
gated: non-enforce modes never raise `GuardBlocked` and never auto-undo.

### A policy predicate (or an undo, or an escalation) can never crash the call it's guarding

Three fail-safe behaviors, all in service of one rule — a bug in *user*
policy/reversible/escalation code must never crash the tool call, and must
never silently disappear from the audit trail:
- **Predicate exceptions fail closed.** `PolicySet.evaluate()` and
  `AgentScopedPolicy.evaluate()` wrap every `rule.predicate(ctx)` call via
  `_safety.safe_call()`; an exception becomes a synthetic `RuleResult(passed=False, on_fail=BLOCK, severity="high", ...)`
  **regardless of the rule's own declared `on_fail`/`severity`** — a broken
  predicate can't be trusted to honor ESCALATE or log-only ALLOW semantics.
  `is_active()` (wrapping `active_when`/`applies_to`) fails safe the other
  direction: an exception there is logged and treated as **active** (`True`),
  never silently skipped.
- **Escalation timeouts are actually enforced.** `RuleResult.timeout_s` used
  to be advisory-only. `_engine._run_with_timeout` (sync engine — runs the
  handler in a disposable one-worker thread pool) and
  `_run_with_timeout_async` (async engine — awaits an `async def escalate`
  directly with `asyncio.wait_for`, or runs a sync `escalate` via
  `loop.run_in_executor` so a blocking call can't freeze the event loop) both
  deny (`False`) on timeout or on any exception from the handler. A sync
  `escalate` handed to the *sync* engine that turns out to be `async def` is
  explicitly detected and denied too — `bool(some_coroutine)` is always
  `True`, so silently `bool()`-coercing it would approve everything.
- **Undo failures don't erase the ledger event.** In both engines, a
  `ReversibleAction.undo()` (or `undo_fn`) exception during a post-BLOCK is
  caught, logged, and folded into `undo_op`/`decision.reason` — the
  `_record()` call and the `raise GuardBlocked(...)` that follow always still
  happen. Losing the audit trail at exactly the moment an undo failed would
  defeat the point of having one.

### Async: auto-detected, not a parallel API

Predicates (`pre`/`post`, `active_when`, `applies_to`) are always plain sync
callables — cheap, deterministic checks, not I/O. Only *tool invocation*,
`ReversibleAction.do_fn`/`undo_fn`, and `EscalationHandler.escalate` may be
async, and it's auto-detected via `inspect.iscoroutinefunction` — there's no
`@guard_async` or separate async policy class to remember:
- `@guard` returns an `async def` wrapper (dispatching to
  `evaluate_call_async`) if the wrapped function (or, for a
  `ReversibleAction`, its `do_fn`) is a coroutine function; otherwise today's
  plain sync wrapper. `await` it like you would the original function.
- `TollgateInterceptor.acall(...)` is the async sibling of `.call(...)`.
  `wrap_tool()` (and therefore `.use(agent)`, for agents with mixed sync/async
  tools) auto-detects the same way and returns a matching sync or async
  wrapped callable.
- `_engine._maybe_await(value)` (`await value` if `inspect.isawaitable(value)`,
  else return it as-is) is what lets a single `do_fn`/`undo_fn`/`escalate`
  implementation be either `def` or `async def` without a second base class.

### Ambient identity flows through `contextvars`, not function arguments

`@guard`-decorated functions keep their normal Python signature (no `ctx`
parameter), so caller identity / session / domain data has to come from
somewhere else: `tollgate._scope.ExecutionScope`, propagated via a
`contextvars.ContextVar`. `TollgateInterceptor.call()` builds a fresh scope per
call (incrementing `step_index`, pulling role/trust/delegation from its
`TollgateRegistry` if configured) and installs it with `use_scope()` for the
duration of the call — so a `@guard`-decorated helper invoked *from inside* an
intercepted tool still sees the same caller identity. Bare `@guard()` usage
outside any interceptor reads whatever `ExecutionScope` is ambient, which a
caller can set explicitly via `tollgate.session(caller_role=..., ...)`.

### Decisions are resolved with explicit precedence, not first-match

When multiple rules fail for the same hook, the engine doesn't just take the
first one — `decisions.pick_decision()` picks the failure with the highest
precedence: BLOCK > ESCALATE > ALLOW. An `on_fail=ALLOW` rule is a "log-only"
rule (shown as a `log` edge in the coverage graph): it still gets recorded to
the ledger when it fails, but it never blocks or escalates anything, even if
it "fires" at the same time as a BLOCK rule (which wins).

### An authorized tool that raises is still an audit event

`invoke()` is wrapped in both engines: if the guarded tool raises, the engine
records a `decision="ERROR"`, `hook="invoke"` `LedgerEvent` (exception type and
message in `reason`, full caller identity attached) and re-raises unchanged.
Otherwise a call that was authorized, ran, and blew up would skip every
post-hook *and* the recording step, leaving no trace at all. `"ERROR"` lives
only on `LedgerEvent.decision` — `Decision`/`GuardDecision` stay
BLOCK/ESCALATE/ALLOW, because this is not a policy decision. It is never
sampled, and emits no OTEL span: Tollgate decided nothing here, and whatever
instruments the tool owns that part of the trace. Post-hooks are deliberately
skipped — they have no result to inspect.

### Every failing rule is recorded, not just the one acted on

`pick_decision()` collapses a hook's failures to one `RuleResult` by precedence,
and that one drives the outcome. But the ledger records *all* of them: the
engine attaches the full failing list to `GuardDecision.rule_results`, and
`_record()` writes it out as `LedgerEvent.contributing_rules`. Recording only
the winner meant an audit could not ask "what else was wrong with this call?"
— three rules failing at once looked identical to one. Every `RuleResult` also
carries the `policy_hash` of the policy that produced it, stamped in
`PolicySet.evaluate()`/`AgentScopedPolicy.evaluate()`, so a decision can be
traced back to the exact policy version that made it. Because that hash is now
read on every call, `PolicySet.policy_hash` memoizes its digest and invalidates
the cache in `require()`.

### `Policy` is the common interface across very different rule shapes

`PolicySet` (ANDed `require()` rules), `AgentScopedPolicy` (role-gated single
rule), and the composite wrappers `AndPolicy`/`OrPolicy`/`NotPolicy` (built by
`&`/`|`/`~`) all implement `core/policy_set.py:Policy` — `is_active(ctx)`,
`evaluate(ctx, hook)`, `policy_hash`. The engine, linter, and reports only ever
talk to this interface, so a new policy shape can be added without touching
any of them. Composite semantics (a pragmatic design choice — nothing forces
this interpretation of `&`/`|`/`~` other than internal consistency):
- `&` is active if either child is, and concatenates both children's results
  (failing any rule from any active child fails the AND).
- `|` is active if either child is, passes if at least one active child fully
  passes, and otherwise reports every active child's failures together.
- `~` inverts the child as a whole: fails if the child raised *no* violation
  for that hook, passes if the child had at least one failing rule.

### `ReversibleAction`'s irreversibility level is enforced as an intrinsic rule

`ReversibleAction.intrinsic_check()` returns a synthetic, always-firing
`RuleResult` for `"permanent"` (unconditional BLOCK) and `"high"`
(unconditional ESCALATE) — `None` for `"low"`/`"medium"`. The engine prepends
this to the normal pre-hook rule list when the call wraps a `ReversibleAction`,
so irreversibility is enforced the same way as any other policy, with the same
ledger/span treatment. `"medium"` without an `undo_fn` raises `ValueError` at
construction time (import-time), not at call time.

### Escalation: pluggable handler, safe default, real handlers ship separately

`core/escalation.py` resolves an `escalate_to="scheme://..."` URI's scheme
against handlers registered via `register_handler(scheme, handler)`. With
nothing registered (the default for every scheme), `FailSafeEscalationHandler`
logs a warning and denies — an escalation that can't actually be resolved must
never be silently treated as an approval. The ledger/span `decision` for an
escalated rule stays `"ESCALATE"` regardless of approval outcome; the
approve/deny detail is folded into the `reason` text, and the engine separately
tracks whether the call should actually proceed. `EscalationHandler.escalate()`
may be `def` or `async def` (see "Async", above) — the engine's `timeout_s`
enforcement applies either way, so implementations don't need to manage their
own timeout. `core/escalation.py` itself only has the ABC + registry + safe
default — real implementations live in `tollgate.escalation` (see below), kept
separate since they're additive and, unlike the default, do real I/O.

### Real escalation handlers: Slack, webhook, CLI

`src/tollgate/escalation/{slack,webhook,cli}.py` — three `EscalationHandler`
implementations, each a genuinely different approval-channel shape. All use
only `urllib.request`/stdlib (no new dependency), are plain `def escalate(...)`
(sync — verified sufficient under both engines per the confirmed
`_run_with_timeout*` behavior above), and are exported from `tollgate/__init__.py`
directly (safe to import eagerly — zero external dependency, unlike the
framework adapters). Unlike the framework adapters, these are **not**
auto-registered — there's no way to "detect" that an agent wants Slack; call
`register_handler(scheme, handler)` explicitly (see
`examples/real_escalation_handlers.py`).

- **`SlackEscalationHandler`**: Slack has no synchronous "click here, get the
  answer back in this HTTP response" primitive without also running a public
  webhook receiver, so this polls instead — post via `chat.postMessage`, then
  poll `reactions.get` for a ✅/❌ from an allowlisted approver.
  `approvers` (Slack user IDs) is a **required** constructor argument — without
  an allowlist, anyone reacting in the channel would approve.
- **`WebhookEscalationHandler`**: one synchronous POST to a configured `url`,
  expects `{"approved": bool}` back. `headers` is where the caller's own auth
  goes (shared secret, bearer token).
- **`CLIEscalationHandler`**: local `input()`-based human-in-the-loop, no
  network I/O — for local dev/demos/CLI-driven agents, not a deployed agent
  with no one at a terminal.
- **Handler-level `timeout_s`, independent of the per-rule one:** each takes
  its own `timeout_s` constructor parameter (a sensible default) — `Slack`/
  `Webhook` use `min(self.timeout_s, rule_result.timeout_s)` for their actual
  internal wait (the polling deadline / the HTTP request timeout), so the
  integration has a configured ceiling that's never exceeded regardless of
  what a specific `@guard(timeout_s=...)` call declares, and vice versa.
  `CLIEscalationHandler`'s `timeout_s` is informational only (shown in the
  prompt) since stdlib `input()` can't be cleanly interrupted mid-call — the
  real cutoff there still comes from the engine abandoning the blocked call.

### Multi-agent: identity lives on the interceptor or the registry, never on the tool

In multi-agent systems, delegation between agents can silently escalate
privilege: agent A delegates to agent B, B has access A doesn't, and no policy
ever explicitly authorized that escalation (the "confused deputy" problem). A
collection of individually-safe agents doesn't guarantee a collectively-safe
system. Tollgate's answer: share the tool, never share an interceptor without
identity — policies decide based on *who* called, not just *what* was called.
Two supported patterns:
- **Pattern 1 (default, most cases):** one `TollgateInterceptor` per agent, each
  constructed with its own `agent_id` and policy list. The exact same tool
  function passed to two interceptors can be BLOCK for one agent and ALLOW for
  another, because `ctx.caller_role` differs — see `examples/clinical.py`.
- **Pattern 2 (many/dynamic agents):** one shared `TollgateRegistry` that
  multiple interceptors all reference by `agent_id`, instead of duplicating
  role/policy wiring per interceptor.
- **`delegation_chain` has one convention: register ancestors, read the full
  path.** `TollgateRegistry.register(delegation_chain=...)` takes the agent's
  *ancestors only*; `TollgateInterceptor._self_inclusive_chain()` appends the
  acting `agent_id` when building the `ExecutionScope`, so every
  `GuardContext`, ledger event and graph sees the complete lineage
  (`["orchestrator", "executor_agent"]` — the shape the ledger documents).
  The two used to disagree: `report/graph.py`'s `zip(chain, chain[1:])` drew
  **zero** edges for a direct parent→child hop, and `_is_cross_agent` never
  fired on a single delegation. `delegation_depth()` counts *hops*
  (`len(chain) - 1`), which is why appending the agent didn't shift any
  existing `max_delegation_depth_policy` threshold. A chain that already ends
  with the agent's own id is left alone, so code written against the old
  convention keeps working.
- **Anti-pattern:** a `TollgateInterceptor` with no `agent_id` (or no
  registry) leaves `ctx.caller_role` always `None`, silently defeating any
  `AgentScopedPolicy` with `allowed_roles` set. `tollgate lint` flags this as
  an `error`-severity finding.

### `tollgate.policies` — the shipped policy library

`src/tollgate/policies/` holds tested, versioned implementations of the rules
every agent re-derives: `no_secrets_in_args`, `no_destructive_sql`,
`no_destructive_shell`, `path_within`, `domain_allowlist`,
`rate_limit_policy`, `budget_policy`. Each is a *constructor* returning an
ordinary `PolicySet`, so nothing in the engine, linter or reports needs to
know they exist and they compose with `&`/`|`/`~` like hand-written policies.

Design rules they all follow, and any new one should:
- **Take `tool_names`** (or `tool_name`) and translate it into `active_when`.
  An unscoped policy runs against every tool through the same interceptor —
  the "common pitfall" below.
- **Fail closed on a missing argument.** `path_within` with no `path` in
  `ctx.args` blocks; it has verified nothing, so it must not pass.
- **Resolve before comparing.** `path_within` calls `Path.resolve()` (handles
  `..` *and* symlinks) and `domain_allowlist` compares the parsed
  `urlsplit().hostname`, never a substring of the URL — a substring check
  would let `example.com.evil.com` through an `example.com` allowlist.
- **Anchor detection on literal markers, not entropy.** `secrets.py` matches
  `sk-ant-`, `AKIA`, `-----BEGIN`; that keeps false positives rare enough for
  BLOCK to be a sane default.

They are seatbelts against agent mistakes and opportunistic prompt injection,
**not** a sandbox — the SQL/shell matchers are evadable by an adversary who
controls the input exactly, and detection is not redaction (a matched secret
still lands in `LedgerEvent.args`).

### `CallState`: cross-call history, deliberately beside the context

`rate_limit_policy`/`budget_policy` need memory across calls, which the
stateless `GuardContext` can't hold — CLAUDE.md previously listed both as
deferred for exactly that reason. `src/tollgate/state.py:CallState` resolves
it without compromising the data model: the counters live in a separate,
lock-guarded, LRU-bounded object injected into `ExecutionScope` the same way
`checksum_provider`/`consent_provider` already are, and reached through
`ctx.calls_this_session()` / `ctx.spent()` / `ctx.record_spend()`.

Two consequences worth keeping:
- **`GuardContext` stays a value.** No history is stored on it; it holds a
  reference to state owned by the interceptor.
- **Replay stays deterministic.** `ledger.replay()` builds a scope with no
  `CallState`, so a history-dependent policy reads zero rather than silently
  consulting today's live counters. All the read helpers return `0`/`0.0` in
  that case, which means these policies *allow* on absent history.

The engine calls `record_call()` before any rule runs, so a rate-limit
predicate sees the call it is deciding on, and counting is on **attempts** —
a blocked call still counts, or retrying a denial would be free.
`record_spend()` is the one mutating method on `GuardContext`;
`budget_policy` calls it from a `hook="post"` rule, so only calls that
actually ran are charged.

### Real framework adapters: LangGraph, OpenAI Agents SDK, and MCP

`adapters/langgraph.py` and `adapters/openai_agents.py` are real
implementations (not skeletons), **registered by default** —
`src/tollgate/adapters/__init__.py` calls `register_adapter()` for both the
first time `interceptor.use()`/`tollgate.wrap()` runs (importing the
`adapters` package, which happens lazily inside `TollgateInterceptor.use()`).
This is safe without adding hard dependencies: `applies_to()` only *attempts*
the optional `langchain_core`/`agents` import lazily, inside the method body
— `import tollgate` never imports either. Install the extras to use them:
`uv sync --extra langgraph` / `--extra openai-agents`.

Both accept `agent` as either a bare `list[Tool]` (wrap *before* constructing
the graph/agent: `wrapped = tollgate.wrap(my_tools, interceptor)`) or an
object with a `.tools: list[Tool]` attribute (wrapped in place) — not every
possible framework configuration shape (e.g. a `.get_tools()` method), a
deliberate scoping decision.

- **LangGraph** (`langgraph.py`): LangGraph doesn't have its own tool type —
  `create_react_agent`/`ToolNode` both consume plain
  `langchain_core.tools.BaseTool` instances, so this adapter really wraps
  *LangChain* `BaseTool` objects (covering any LangChain tool, in or out of
  LangGraph). Wraps through `.invoke()`/`.ainvoke()` — the two
  guaranteed-stable public entry points on every `BaseTool` (part of
  LangChain's `Runnable` interface) — by building a *new*
  `StructuredTool.from_function(func=..., coroutine=..., args_schema=tool.args_schema, infer_schema=False, ...)`,
  rather than monkeypatching `_run`/`_arun` on an arbitrary subclass.
  `ainvoke` is always wired (LangChain gives every `BaseTool` a default
  async-over-sync fallback even if the original tool is sync-only).
- **OpenAI Agents SDK** (`openai_agents.py`): every `FunctionTool.on_invoke_tool`
  has the same async contract, `(ctx: ToolContext, args_json: str) -> Awaitable[Any]`,
  so this adapter is unconditionally async (`interceptor.acall()`). The
  wrapper parses `args_json` into `ctx.args` for policy evaluation, and on
  approval re-serializes the (possibly policy-mutated) kwargs to call the
  *original* `on_invoke_tool` unchanged, preserving whatever validation the
  SDK generated. Built via `dataclasses.replace(tool, on_invoke_tool=...)`
  (`FunctionTool` is a dataclass). Only entries with an `on_invoke_tool`
  attribute are wrapped — hosted/built-in tools (`WebSearchTool`,
  `FileSearchTool`, ...) pass through untouched, since Tollgate can't
  meaningfully guard tool execution it doesn't control. Note: the SDK has its
  own `tool_input_guardrails`/`tool_output_guardrails`/`needs_approval`
  fields — Tollgate is complementary (framework-agnostic policy/ledger/audit
  across every framework), not a replacement for those.
- **MCP** (`mcp.py`): the only adapter that guards *both* ends of a protocol,
  because both are real deployment shapes. `guard_mcp_session` wraps
  `ClientSession.call_tool` (you run the agent); `guard_mcp_server` replaces
  the entry in the low-level `Server.request_handlers[CallToolRequest]` table,
  reached through `FastMCP._mcp_server` when given a `FastMCP` (you run the
  server). `MCPAdapter.install()` dispatches between them.
  **The two report a denial differently, and that asymmetry is load-bearing:**
  the client side raises `GuardBlocked` into your own calling code, while the
  server side returns `CallToolResult(isError=True)` — an exception escaping a
  request handler tears down the protocol connection for every *subsequent*
  request, so a single denial would take the server down. Both wrappers mark
  the object with `__tollgate_mcp_guarded__` so a second `use()` doesn't stack
  a second evaluation layer (which would double-count against a rate limit).
  Guarding a server with no `tools/call` handler registered yet raises
  explicitly rather than silently guarding nothing — wrap *after* defining
  tools.
- **Common pitfall, not a library bug:** a `PolicySet` with no `active_when`
  applies to *every* tool call through the same interceptor. Wrapping several
  tools with different arg shapes through one shared `policies=[...]` list
  means an unscoped predicate referencing e.g. `ctx.args["amount"]` will
  `KeyError` on a call to a tool without that field — correctly caught and
  turned into a fail-safe `BLOCK` (see "can never crash the call it's
  guarding", above), but not what you want. Scope with
  `active_when=lambda ctx: ctx.tool_name == "..."` — see
  `examples/langgraph_integration.py`/`examples/openai_agents_integration.py`,
  which both hit exactly this while being written and fixed it the same way.

### Coverage combines dynamic (ledger) and static (synthetic-context) signals

`report/policy_report.py:build_report()` computes "coverage" from two sources,
by default: *dynamic* — a tool that has at least one policy-attributed
`LedgerEvent` recorded — and *static* — a tool that some policy's
`is_active()` applies to right now, checked the same way `linter/linter.py`
does (a synthetic `GuardContext` per tool name). A freshly loaded agent module
with zero calls made still shows correct coverage for any tool a policy
statically applies to; `block_count`/`escalate_count`/`allow_count` per policy
remain purely ledger-driven (0 until something actually happens; see below for
the recency window). Pass `include_static=False` (`tollgate report
--no-static-coverage`) for the old audit-only view: only tools with recorded
activity count as covered. Static and dynamic *can* still disagree in one
direction — a `PolicySet` whose `active_when` checks `ctx.domain` only counts
as statically covering a tool if the synthetic context happens to have that
domain set, which by default it doesn't (`ExecutionScope()` defaults).

Per-policy `block_count`/`escalate_count`/`allow_count` are windowed —
`window_hours` (default `DEFAULT_WINDOW_HOURS = 24.0` in `policy_report.py`,
`--window-hours` on the CLI) controls how far back events are counted; the
resulting `PolicyReport.window_hours` is echoed back so consumers know what
window a given report used.

### `src/tollgate/testing/` ships to users — it is not this repo's test suite

`src/tollgate/testing/harness.py` (`fixtures_from_events`, used by
`ActionLedger.export_fixtures()`) and `src/tollgate/testing/repl.py`
(`evaluate_synthetic`, used by `tollgate repl`) are **library code**: testing
*utilities* the framework exposes to people building on top of Tollgate, the
same way `pytest` ships fixtures or Django ships `django.test`. They install
with the package and are part of the public API surface. Tollgate's own
tests, which exercise this repo's code, live under `/tests` at the repo root
and are excluded from the built package (see `pyproject.toml` / `uv build`).
Don't confuse the two: `src/tollgate/testing/` is not misplaced, and moving it
into `/tests` would remove it from every installed copy of the library.

### The CLI's module-loading convention

`tollgate report` / `lint` / `repl` load the target file as a plain Python
module (`importlib.util`) and look for two conventional module-level names:
`POLICIES: list[Policy]` (required) and, optionally, `REGISTRY:
TollgateRegistry` / `TOOL_NAMES: list[str]` / `ACTIONS: list[ReversibleAction]`
(the last one only feeds `lint`'s `"high"`-without-`escalate_to` check). This is not enforced by any base
class — it's just what `cli/main.py` greps for. See `examples/clinical.py` for
the convention in practice. `tollgate report --ledger path.jsonl` merges
events from a JSONL ledger sink file (written via
`ActionLedger.configure(sink_path=...)`) with whatever's in the current
process's in-memory ledger.

### The process-wide ledger is configured, not constructed

`ActionLedger.current()` is what `_engine._record()` writes to, and it builds
its lazy singleton with *no arguments* — so constructing
`ActionLedger(sink_path=...)` yourself produces an object nothing ever
consults. `ActionLedger.configure(sink_path=..., max_events=...)` (exported as
`tollgate.configure_ledger`) is the only supported way to set the sink for the
ledger the engine actually uses. Call it once at startup, before the first
guarded call; events already recorded stay with the old ledger and are not
migrated. Singleton creation and replacement are both guarded by
`_singleton_lock`, separate from each instance's own `_lock`.

### Sampling

**Sampling is rolled exactly once per event**, by `otel.spans.should_sample()`,
and the result is passed into `evaluate_span(sampled=...)` — that function
never rolls its own. Before this, `_record_allow` rolled for the ledger and
`evaluate_span` rolled again at the same rate, making the effective span rate
`allow_sample_rate²` and leaving the ledger and the traces disagreeing about
which events survived.

A failing rule (BLOCK, ESCALATE, or a log-only ALLOW) is always recorded to the
ledger; only its span is sampled, at `block_sample_rate`. "Failing" means
anything that isn't an ALLOW — an ESCALATE samples as a block, not as an allow.
When every applicable rule for a hook passes, a single aggregate ALLOW ledger
entry is recorded, sampled at `OtelSettings.allow_sample_rate` (default 1.0),
and that one roll governs both the ledger entry and its span. The aggregate
entry's `policy` field is the contributing policy's name if exactly one policy
produced rules for that hook, a `+`-joined label if several did, or `None` if
none did (in which case nothing is recorded at all — see
`_engine.py:_record_allow`); `policy_hash` is only reported in the
single-policy case, since a `+`-joined label has no one fingerprint.

A tool that raises is recorded unconditionally and never sampled — see "An
authorized tool that raises is still an audit event".

## Scope: what's implemented vs. deferred

Implemented: `@guard` (sync and async, auto-detected), `PolicySet`/
`AndPolicy`/`OrPolicy`/`NotPolicy`, `ReversibleAction` (sync or async
`do_fn`/`undo_fn`), `GuardContext`, `TollgateInterceptor`
(enforce/dry_run/observe, sync `.call()` and async `.acall()`,
per-interceptor `otel_tracer` override, bounded session-counter memory via
`max_sessions`), `ActionLedger` (JSON/CSV export, DOT/Mermaid/JSON policy +
delegation graphs, natural-language narrative export, replay, bounded
in-memory ring buffer via `max_events` with lossless `sink_path` mirroring),
`TollgateRegistry` / `AgentScopedPolicy` / delegation helpers, OTEL spans +
per-event counters/histograms (`otel/config.py` degrades to no-ops without
`opentelemetry` installed or a reachable tracer/meter provider), a policy
linter (dead policies, duplicate names, the scoped-policy-without-registry
anti-pattern, uncovered tools), a ledger-driven pytest fixture generator, a
minimal synthetic-context REPL, and the `tollgate` CLI
(`report`/`lint`/`replay`/`repl`). Compliance/audit data is exportable as
JSON, CSV, DOT, Mermaid, and a natural-language narrative — deliberately not
as PDF; those formats cover the "get this data out in a reviewable form" need
without a PDF-rendering dependency. Policy predicates, `ReversibleAction.undo`,
and `EscalationHandler.escalate` all fail closed on an exception rather than
crashing the guarded call or losing the ledger entry (see "A policy predicate
... can never crash the call it's guarding", above); `RuleResult.timeout_s` on
escalation is actually enforced, not advisory. Real `EscalationHandler`
implementations ship under `tollgate.escalation`: `SlackEscalationHandler`,
`WebhookEscalationHandler`, `CLIEscalationHandler` (see "Real escalation
handlers", below) — plus the LangGraph and OpenAI Agents SDK framework
adapters (see "Real framework adapters", above). Packaging: `LICENSE`
(Apache-2.0), GitHub Actions CI (lint/typecheck/test across Python
3.11–3.13, then build), `CONTRIBUTING.md`, `SECURITY.md`.

Deferred or intentionally out of scope:
- **Live OTEL gauges** for `block_rate`/`escalate_rate`/`coverage_ratio` —
  these windowed stats are computed on demand by `report/policy_report.py`
  from the ledger instead (see its module docstring), not pushed as
  continuously-updating OTEL gauge instruments.
- **General logical-conflict detection between policies** in the linter —
  `linter/linter.py` catches dead policies (zero rules), duplicate names, and
  the scoped-policy-without-registry anti-pattern, but does not attempt to
  prove two arbitrary policies contradict each other.
- **Policy version diffing in the ledger** — `PolicySet.policy_hash` gives a
  deterministic fingerprint per policy, but there's no automatic "policy X
  changed from hash A to hash B" ledger entry when rules change between runs.
- **OTEL span enrichment beyond summary attributes** — spans carry
  tool/policy/policy_hash/action/hook/severity/reason/latency/session/step/
  caller/trust/delegation_chain/dry_run, plus an ERROR span status on an
  enforced BLOCK (see `otel/spans.py`) — but not a serialized predicate
  source, a snapshot of context values at evaluation time, or the tool call's
  stack frame.
- **Automatic test-input synthesis** — `testing/harness.py` generates pytest
  fixtures by harvesting real `(policy, tool, decision)` combinations already
  recorded in the ledger, not by synthesizing novel passing/failing inputs for
  arbitrary predicates (which would need constraint-solving over arbitrary
  lambdas — not attempted).
- **Policy hot-reload / rollback**, and **anomaly-rate alerting** (detecting a
  sudden shift in block rate) — both production-operations features, not
  built.
- **History-based and environment-conditional policies** — e.g. "block if
  this tool was called more than N times this session," or policies that
  branch on `staging` vs `production` — both considered and intentionally not
  built: the former needs cross-call state that complicates the data model,
  the latter risks silent policy divergence between environments.
- **Dedicated adapters for CrewAI, Claude Agent SDK, AutoGen, Pydantic-AI,
  Google ADK, Strands** — not written. There used to be `NotImplementedError`
  skeletons for CrewAI/Claude SDK/LangChain; they were deleted, because a stub
  that raises on `install()` is worse than no stub: `interceptor.use(agent)`
  would find it, claim the agent, and blow up, where the absence lets
  `GenericAdapter` handle it. LangChain needs no adapter of its own — the
  `LangGraphAdapter` wraps `langchain_core.tools.BaseTool`, which is exactly
  what LangChain uses. For the rest, `interceptor.use(agent)` /
  `tollgate.wrap(agent, interceptor)` falls back to `GenericAdapter` (a plain
  `agent.tools` dict/iterable-of-pairs, or a bare callable agent), or call
  `interceptor.wrap_tool()` per tool.
- **TypeScript implementation** — this project is Python-only.
