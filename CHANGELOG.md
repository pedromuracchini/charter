# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) once it
reaches `1.0.0`. Before `1.0.0`, minor versions may include breaking changes.

## [Unreleased]

## [0.2.0] - 2026-08-18

Production-readiness hardening pass, following an adoption audit of the
initial implementation.

This is the first release published to PyPI. `0.1.0` below records the
initial implementation but was never uploaded, so there is no `0.1.0` on the
index to upgrade from.

### Added
- **A typed exception and warning hierarchy** (`charter.errors`). Every
  deliberate failure now derives from `CharterError`, so a caller can catch
  the library's errors without also catching its own bugs. Each class keeps the
  stdlib exception it used to raise — `ConfigurationError` is a `ValueError`,
  `EscalationError` a `RuntimeError`, `LedgerEventNotFound` a `KeyError`,
  `AdapterError` a `TypeError` — so existing `except` clauses still match.
  `ConfigurationWarning` is used for configurations that are legal but almost
  certainly a mistake, which a `logger.warning` left invisible under the
  default logging setup.
- **Per-interceptor `ledger=` and `redactor=`.** One process can now run
  several interceptors with separate audit trails and scrubbing rules instead
  of every call funnelling into the process-wide singletons.
- **`args={...}` on `call()`/`acall()`**, for passing a tool's arguments
  explicitly — see the corresponding fix below.
- **`__repr__` on every public object.** `repr(policy_set)` printed
  `<PolicySet object at 0x7f…>` for a library whose central objects exist to be
  inspected, logged and diffed.
- **`ActionLedger.sink_error_count`**, `charter.reset_ledger()`,
  `charter.reset_otel()`, `charter.reset_redaction()`, `charter.ALL_TOOLS`
  and `charter.policies` are now part of the public surface. `configure_ledger`
  is a real documented function rather than a bare classmethod alias, which
  generated no signature in API docs.
- **A hand-authored architecture guide** (`docs/architecture.md`) and a
  generated API reference (mkdocstrings). The published "Architecture" page was
  previously `CLAUDE.md` verbatim — a page titled "CLAUDE.md" that opened by
  addressing an AI coding agent.

### Fixed
- **`GuardBlocked` did not survive `pickle` or `copy`.** It passed
  `decision.reason` to `Exception.__init__`, and `__reduce__` replays `args` on
  unpickling — so a round trip through Celery, `concurrent.futures` or
  multiprocessing rebuilt `exc.decision` as a bare `str`, turning any later
  `exc.decision.reason` into an `AttributeError`. It now stores the
  `GuardDecision` itself; `__str__` still renders the reason, so `str(exc)` is
  unchanged.
- **A tool argument named `session_id` or `domain` could never reach its
  tool.** Both are named parameters of `call()`/`acall()`, so they bound to the
  interceptor and the call failed with a confusing "missing required argument"
  — and `domain` is an ordinary argument name for a real tool. Arguments can
  now be passed explicitly as `args={...}`, passing a colliding keyword warns
  with `ConfigurationWarning`, and `tool_name`/`func` are positional-only so
  those names are free too. All three adapters forward through `args=`, so a
  wrapped tool is immune by construction.
  **Breaking:** `wrap_tool`-wrapped callables now treat every keyword as a tool
  argument; they previously consumed `session_id`/`domain` as scope.
- **`@guard` reported a signature it could not honor.**
  `functools.update_wrapper` sets `__wrapped__`, so `inspect.signature()`
  returned the original signature while the wrapper accepted keyword arguments
  only. Every framework that introspects a tool to build its JSON schema —
  LangChain, the OpenAI Agents SDK, MCP — reads that signature and emits
  positional calls from it, which failed at runtime with "takes 0 positional
  arguments". Positional arguments are now bound against the real signature.
- **A failing ledger sink turned an allowed call into a crash.** Recording runs
  *after* the guarded tool has executed, so an `OSError` from a full disk or an
  unwritable `sink_path` propagated out of a call the policies had explicitly
  allowed — losing the tool's result to protect a copy of a record that was
  also in memory. Sink failures are now logged and counted in
  `sink_error_count`.
- **Secrets survived redaction inside sets, bytes and dict keys.** Only
  `str`/`Mapping`/`Sequence` were walked, so a credential in a `set`,
  `frozenset`, `bytes` value or dict key reached the ledger, the JSONL sink and
  the Slack escalation message verbatim — contradicting the module's stated
  guarantee. `contains_placeholder()` walks the same shapes, so `replay()`'s
  `redacted` flag cannot under-report.
- **`configure_redaction(include_pii=True, redact_credit_cards=False)` ignored
  the explicit `False`** (it was `redact_credit_cards or include_pii`). The
  parameter now defaults to `None` and follows `include_pii` only when unset.
- **The async escalation path leaked threads and used a deprecated API.** It
  called `asyncio.get_event_loop()` inside a coroutine and dispatched sync
  handlers to the *default* executor; on timeout `wait_for` cancels the future
  but cannot stop the running thread, so an abandoned handler occupied a shared
  worker and could hang `loop.shutdown_default_executor()` at exit. It now uses
  `get_running_loop()` and a disposable pool, matching the sync path. Both
  paths copy the caller's `contextvars`, so a handler reading `current_scope()`
  sees the identity that triggered the escalation.
- **`allow_sample_rate=0.0` could still sample.** `random()` can return exactly
  `0.0` and the comparison was `<=`. Sampling also used the shared `random`
  module, silently consuming the process-wide random stream; it now uses a
  private, lock-guarded RNG.
- **`pick_decision()` broke ties by registration order.** Between two rules that
  both BLOCK, the severity recorded on the ledger event depended on which
  policy happened to be registered first. Ties within a precedence level are
  now broken by severity.
- **A schemeless `escalate_to` silently blocked everything.**
  `resolve_handler()` matches on the URI scheme, so `escalate_to="security-team"`
  matched nothing and fell through to the fail-safe denier. Construction now
  warns, and `register_handler()` rejects a "scheme" that is itself a URI.
- **A tool named `"*"` double-counted against rate limits.** `CallState` used
  `"*"` as the dict key for a session's total; the total now lives in its own
  counter.
- **A corrupt line aborted a whole JSONL ledger read.** `charter report
  --ledger` now skips unparseable lines with a warning — the sink is an
  append-only log a process can be killed partway through writing.
- **The escalation-handler registry and the OTEL instrument cache were
  unsynchronized**, unlike every other shared structure in the package.

### Changed
- **`CharterInterceptor`'s constructor options past `mode` are keyword-only**,
  so their order is no longer frozen.
- **`@guard` preserves the wrapped function's types** via `ParamSpec` instead of
  returning `Callable[..., Any]`. The package ships `py.typed`; a decorator
  that erased types made downstream checking worse than not using the library.
- **`AgentScopedPolicy.policy_hash` is memoized**, and composite policy hashes
  cache keyed on their children's hashes. The former recomputed a SHA-256 on
  every guarded call, which `PolicySet` already avoided.
- **`current_redactor()` no longer takes a lock** to read one module global on
  the path of every recorded event.
- **The linter's `Severity` is now `LintSeverity`** (the old name still
  resolves). `charter.Severity` is a different `Literal` with disjoint values,
  and two exported types sharing a name is a trap.
- **CI installs the library with no extras** in a dedicated job, and runs on
  macOS and Windows and Python 3.14. Every graceful-degradation path was
  previously unreachable in CI, and `path_within` — built on `Path.resolve()`,
  whose semantics differ per platform — had only ever been tested on Linux.
  Coverage is gated, lockfile drift fails the build, wheels are smoke-tested
  and `twine check`ed, releases are SHA-pinned and attested, and CodeQL,
  `pip-audit` and dependency review run on every change.
- **`budget_policy` could not express an LLM token budget.** `amount_from` was
  evaluated at *both* hooks, and at the pre-hook `ctx.result` is still `None` —
  so the natural `lambda ctx: ctx.result["usage"]["output_tokens"]` raised
  `TypeError`, fail-closed, and blocked every call. Added `actual_from`, read
  only in the post hook. With `actual_from` alone the pre-hook check becomes
  "is the budget already exhausted?", so the cap is *stop once spent* rather
  than *never exceed* — inherent to not knowing a price before paying it.
  Passing both bounds the overshoot: the estimate gates the call, the actual
  figure supersedes it when charging. `budget_policy()` with neither argument
  now raises `ValueError` instead of silently doing nothing.
- **`dry_run`/`observe` fired real escalation side effects.** `_engine.py`
  called `_resolve()` — which contacts the escalation handler — *before*
  testing `mode == "enforce"`, so a supposedly no-op rollout would post to
  Slack, hit the approval webhook, or block on `input()` for up to `timeout_s`
  per call, exactly the opposite of what `examples/dry_run_rollout.py`
  promises. Outside `enforce`, the engine now records the ESCALATE it *would*
  have raised (reason suffixed `"(escalation not resolved — ... mode)"`)
  without contacting any handler; the audit trail is unchanged.
  **Breaking (behavior):** a registered handler is no longer invoked in
  `dry_run`/`observe`. Code relying on that side effect must switch to
  `enforce`.
- **`delegation_chain` had two contradictory conventions, so direct
  delegations drew no graph edges.** The registry and `_build_scope()` treated
  the chain as ancestors-only, while `report/graph.py`'s
  `zip(chain, chain[1:])` and the ledger's documented
  `["orchestrator", "executor_agent"]` shape assumed it included the acting
  agent — a parent→child hop is a one-element tuple under the former, which
  yields zero edges. `_is_cross_agent` never fired on a single hop either.
  `CharterInterceptor` now appends its own `agent_id` when building the
  scope (leaving an already self-inclusive chain alone, so the
  `examples/multi_agent_orchestrator.py` workaround keeps working), and
  `delegation_depth()` counts hops (`len - 1`) rather than entries — so every
  existing `max_delegation_depth_policy` threshold keeps its meaning. Callers
  register ancestors only; everything downstream reads the full path.
- **`report --delegation --format json` silently emitted mermaid** instead of
  erroring, hiding the fact that `delegation_graph()` has no JSON writer. It
  now exits with a message naming the supported formats. The JSON report also
  serializes `PolicyStats` via `dataclasses.asdict` rather than `vars()`.
- **ALLOW events were sampled twice, and ESCALATE spans used the wrong rate.**
  `_record_allow()` rolled against `allow_sample_rate` for the ledger, then
  `evaluate_span()` rolled again at the same rate — the effective span rate was
  `allow_sample_rate²`, and the ledger and the traces disagreed about which
  events survived. Separately, `evaluate_span` sampled anything that wasn't a
  BLOCK at `allow_sample_rate`, so dialing allows down silently thinned
  ESCALATE spans too. Sampling is now a single roll via the new
  `otel.spans.should_sample()`, passed into `evaluate_span(sampled=...)`, and
  every non-ALLOW decision samples at `block_sample_rate`.
- **`CharterInterceptor` was not thread-safe.** `_build_scope()`
  read-modify-wrote `_step_counters` (`get` → `+1` → `move_to_end` → possible
  `popitem`) with no lock, so one interceptor shared across request threads —
  the normal server shape — produced duplicate or skipped `step_index` values,
  and concurrent `popitem` could raise. `_wrapped_tools` was mutated unlocked
  too. Both are now guarded by a per-interceptor lock, held only for the dict
  updates and never across policy evaluation or the tool call.
- **Process-global registries leaked between tests and had no way to be
  cleared.** Added `unregister_handler()` / `registered_handlers()` /
  `reset_handlers()` for the escalation registry, `registered_adapters()` /
  `reset_adapters()` / `register_default_adapters()` for the adapter registry,
  and made `reset_otel()` also drop the cached metric instruments (which bound
  to whichever meter provider was live when first created). `register_adapter()`
  now replaces a same-typed adapter instead of stacking duplicates, and its
  docstring no longer claims registration order when the behavior is
  most-recent-first.
- **A post-BLOCK on an action with no `undo_fn` recorded a successful undo.**
  `ReversibleAction.undo()` silently no-ops when `undo_fn is None`, but the
  engine recorded `undo_op="<name>.undo"` and `undo_executed=True` regardless —
  a false success in the audit trail at exactly the moment nothing was
  reverted. The engine now checks the new `ReversibleAction.is_undoable`,
  records `undo_op=None`, appends `"(no undo_fn configured — action NOT
  reverted)"` to the reason, and logs a warning.
- **A tool raising after being authorized left no ledger entry at all.**
  `invoke()` was called bare, so an exception skipped every post-hook and the
  whole recording step: an authorized call that ran and failed was invisible to
  an audit. The engine now records a `decision="ERROR"`, `hook="invoke"` event
  carrying the exception type, message and full caller identity, then
  re-raises unchanged. `LedgerEvent.decision` gained `"ERROR"` and `hook`
  gained `"invoke"`; `Decision`/`GuardDecision` are unchanged, since this is
  not a policy decision.
- **`policy_hash` was never populated in any ledger event or span.** The field
  existed on `LedgerEvent`, the `charter.policy_hash` span attribute was
  emitted, and `PolicySet.policy_hash` was implemented and tested — but the
  engine built every `GuardDecision` without it, so the value was permanently
  `None`. `RuleResult` now carries `policy_hash`, stamped by
  `PolicySet.evaluate()`/`AgentScopedPolicy.evaluate()` and propagated through
  the engine. An aggregate ALLOW reports the hash only when exactly one policy
  contributed. `PolicySet.policy_hash` is memoized (invalidated by `require()`)
  since it is now read on the hot path.
- **`GuardDecision.rule_results` was never populated, so simultaneous failures
  vanished.** Only the single worst rule survived `pick_decision()`; if three
  rules failed at once, the audit trail recorded one. The engine now attaches
  every failing rule, and `LedgerEvent` gained `contributing_rules:
  list[ContributingRule]` recording each one's policy, reason, `on_fail`,
  severity and hash.
- **The ledger's `sink_path` was unreachable through the public API.**
  `ActionLedger.current()` built its lazy singleton with no arguments and
  nothing could replace it, so a hand-constructed `ActionLedger(sink_path=...)`
  was never the ledger the engine wrote to — the documented "full lossless
  history requires `sink_path`" was not actually achievable. Added
  `ActionLedger.configure(sink_path=..., max_events=...)`, exported as
  `charter.configure_ledger`, plus a read-only `sink_path` property.
  `current()` is now created under a lock (two threads racing on the first call
  each built a ledger, and one set of events was silently lost).
- **`ReversibleAction(irreversibility_level="high")` could never escalate.**
  Its intrinsic `RuleResult` was built with `escalate_to=None`, which
  `resolve_handler` maps to the fail-safe denier — so `"high"` was in practice
  a synonym for `"permanent"`, contradicting the documented "auto-escalates
  before every execution". `ReversibleAction` now takes `escalate_to` and
  `timeout_s` and propagates both into the intrinsic check. `charter lint`
  gained a warning for a `"high"` action with no `escalate_to`, fed by an
  optional module-level `ACTIONS: list[ReversibleAction]`.
- **`NotPolicy` (`~policy`) incorrectly blocked on hooks the child policy
  doesn't apply to.** `PolicySet.evaluate()` always includes one `RuleResult`
  per matching rule, whether it passed or failed — an *empty* result
  unambiguously means "not applicable" (inactive, or no rules registered for
  this hook), never "applicable and everything passed". `NotPolicy.evaluate()`
  didn't make that distinction: a child with only `hook="pre"` rules produced
  an empty (not-applicable) result on the `"post"` hook, which `NotPolicy`
  misread as "child raised no violation" and synthesized a block for — so
  `~policy` would spuriously block on hooks the underlying policy was never
  even meant to run on. Found while writing `examples/policy_composition.py`.
  Now: an empty child result makes `NotPolicy` not-applicable too (`[]`).

### Added
- **Token and cost policies for LLM tool calls** (`charter.policies.cost`):
  `token_budget_policy` (dollars per session), `token_limit_policy` (raw
  tokens), plus the `token_cost` / `token_count` / `extract_usage` building
  blocks. `extract_usage` reads the Anthropic (`input_tokens`), OpenAI
  (`prompt_tokens`) and Google (`promptTokenCount`) shapes, from mappings or
  SDK objects, and treats a missing usage block as zero rather than failing
  closed. Prices are parameters quoted per million tokens — no pricing table
  ships, because a stale constant in a security library would silently
  mis-bill.

- **Redaction of secrets and PII on the way into the audit trail**
  (`charter.redaction`). Tool arguments previously reached the in-memory
  ledger, the JSONL sink on disk, the JSON/CSV exports and the Slack
  escalation message completely verbatim, so an agent passing an API key
  leaked it into all four at once.
  **Breaking (default behavior):** credential-shaped values and values under
  names like `password`/`api_key`/`authorization` are now replaced with
  `[REDACTED]` before being recorded. Free-text fields (`reason`, `undo_op`,
  `contributing_rules[].reason`) are scrubbed too, since a fail-closed
  predicate folds exception text — which routinely quotes the offending
  argument — into the reason.
  Redaction happens at *record* time: policies still evaluate against the real
  `ctx.args`, or a predicate written to check a credential could not check it.
  PII patterns (email, SSN, IBAN, IP, formatted phone, Luhn-validated card
  numbers) are opt-in. Configure with
  `charter.configure_redaction(enabled=..., keys=..., include_pii=...,
  extra_patterns=..., redactor=...)`; pass your own `Redactor` to delegate to
  an existing DLP service.
  Consequence: `replay()` rebuilds its context from the stored event, so
  replaying a redacted call evaluates policies against placeholders.
  `ReplayResult.redacted` flags this and logs a warning, and
  `fixtures_from_events` emits `@pytest.mark.skip` for those events rather
  than generating tests that cannot pass.
- **Escalation summaries are capped at `MAX_ARGS_CHARS` (2,000).** Slack
  rejects a message over 40,000 characters outright, so a tool with a large
  payload would previously turn every escalation on it into a silent delivery
  failure.
- **CLI: `--version`, `report --fail-under`, and an `export` command.**
  `report` always exited 0, so it was useless as a CI gate; `--fail-under
  RATIO` now exits non-zero when tool coverage falls below the threshold (the
  report is still printed, so CI logs show what failed).
  `charter export --format json|csv|narrative|fixtures [--output PATH]`
  reaches `export_compliance_report`, `narrative()` and
  `fixtures_from_events()`, none of which had a CLI command before. `lint`
  takes `--agent` like every other subcommand, still accepting its historical
  positional form.
- **`charter.policies` — a library of ready-made policies.** Previously every
  policy was something you wrote from a blank lambda, so each project
  re-derived the same handful of rules. Ships `no_secrets_in_args`,
  `no_destructive_sql`, `no_destructive_shell`, `path_within`,
  `domain_allowlist`, `rate_limit_policy` and `budget_policy`, each returning
  an ordinary `PolicySet` that composes with `&`/`|`/`~`. All take
  `tool_names` for scoping and fail closed on a missing argument;
  `path_within` resolves before comparing (so `..` and symlink escapes are
  caught) and `domain_allowlist` matches the parsed hostname (so
  `example.com.evil.com` can't slip past an `example.com` entry).
- **`CallState` — cross-call counters and spend accumulators**
  (`charter.state`), backing the rate-limit and budget policies that were
  previously listed as deferred. Lives beside `GuardContext` rather than in
  it: a lock-guarded, LRU-bounded object injected into the `ExecutionScope`
  like the existing checksum/consent providers, read through
  `ctx.calls_this_session()` / `ctx.spent()` / `ctx.record_spend()`. Replay
  reconstructs a scope without one, so history-dependent policies report zero
  instead of reading live counters. `CharterInterceptor` gained a
  `call_state` parameter — pass a shared instance to enforce one quota across
  several agents.
- **MCP adapter** (`charter[mcp]`), guarding `tools/call` on both sides of
  the protocol: `guard_mcp_session` wraps a `ClientSession` (a denial raises
  `GuardBlocked`), `guard_mcp_server` wraps the server's registered
  `tools/call` handler (a denial returns `CallToolResult(isError=True)`, since
  an exception escaping a request handler would tear down the connection for
  every later request). Auto-detected by
  `interceptor.use()`/`charter.wrap()`. New example
  `examples/mcp_integration.py` and tests against real MCP objects over an
  in-memory transport.
  **Both mcp 1.x and 2.x are supported**, detected from the installed package
  rather than configured: 2.0 renamed `FastMCP` to `MCPServer`, moved the
  low-level handle to `_lowlevel_server`, replaced the request-type-keyed
  handler table with a method-keyed one behind
  `get_request_handler`/`add_request_handler`, changed the handler contract to
  `(ctx, params) -> CallToolResult`, and renamed the result flag to
  `is_error`. `guard_mcp_session` also accepts 2.x's `Client` facade and
  guards the session underneath it.
- **Escalation metrics**, which did not exist at all: `charter.escalations_total`
  (attributed with `outcome` — `approved` / `denied` / `not_resolved` — plus
  policy, tool and `escalate_to`) and `charter.escalation_latency_ms`. An
  escalation is the one decision that puts a human in the request path, so its
  rate, approve/deny split and wait time are the highest-value operational
  signals Charter can emit.
- **`charter.delegation_depth` is now actually emitted.**
  `record_delegation_depth()` existed and was documented but no call site ever
  invoked it. The engine now records it per call, attributed to the calling
  agent.
- **Richer OTEL spans**: `charter.tool` (previously absent — spans could not
  be grouped by tool in a backend), `charter.session_id`,
  `charter.step_index`, `charter.trust_level`, and an ERROR span status on
  an enforced BLOCK so denials surface in a trace UI's error views (not set in
  `dry_run`/`observe`, where nothing was actually denied).
- **Real `EscalationHandler` implementations** under `charter.escalation`:
  `SlackEscalationHandler` (posts via `chat.postMessage`, polls
  `reactions.get` for a ✅/❌ from an allowlisted approver — `approvers` is a
  required constructor argument, not optional), `WebhookEscalationHandler`
  (one synchronous POST, expects `{"approved": bool}` back), and
  `CLIEscalationHandler` (local `input()`-based human-in-the-loop). All three
  use only `urllib.request`/stdlib — no new dependency or pyproject extra.
  Each takes its own `timeout_s` constructor parameter (independent of any
  given rule's own `timeout_s` from `@guard(timeout_s=...)`) — `Slack`/
  `Webhook` bound their actual wait to `min(self.timeout_s, rule_result.timeout_s)`;
  `CLIEscalationHandler`'s is informational only (stdlib `input()` can't be
  cleanly interrupted mid-call). Exported from `charter/__init__.py`. New
  example `examples/real_escalation_handlers.py` (Slack section mocks the
  Slack API in-process; webhook section runs a real local HTTP server; CLI
  section uses scripted input — all three genuinely runnable without external
  credentials) and new tests under `tests/escalation/`.
- **Real LangGraph and OpenAI Agents SDK adapters**, replacing the
  `NotImplementedError` skeletons — `adapters/langgraph.py` wraps
  `langchain_core.tools.BaseTool` objects (the type every LangGraph tool is)
  through `.invoke()`/`.ainvoke()`; `adapters/openai_agents.py` wraps
  `agents.FunctionTool.on_invoke_tool`. Both accept a bare `list[Tool]` (wrap
  before constructing the graph/agent) or an object with `.tools`, and are
  **registered by default** (`src/charter/adapters/__init__.py`, new) —
  `interceptor.use(agent)`/`charter.wrap(agent, interceptor)` now auto-detect
  them, no manual `register_adapter()` call needed. New optional dependency
  groups `charter[langgraph]` and `charter[openai-agents]`. New examples
  `examples/langgraph_integration.py` and `examples/openai_agents_integration.py`,
  and new tests under `tests/adapters/` (skipped gracefully via
  `pytest.importorskip` when the extras aren't installed) exercising the
  *real* framework objects, not mocks.
- 8 new runnable examples under `examples/`: `quickstart.py`,
  `policy_composition.py` (`&`/`|`/`~`), `dry_run_rollout.py`,
  `custom_escalation_handler.py`, `reversible_levels.py`,
  `delegation_chain.py`, `multi_agent_orchestrator.py` (a centralized
  registry with 4 agents, role + trust-level policies, and an exported
  delegation graph), `audit_and_reporting.py`. See the table in
  `README.md`'s new "Examples" section.
- **Async support.** `@guard` and `CharterInterceptor.wrap_tool()`/`.use()`
  auto-detect an `async def` tool function (or a `ReversibleAction` with an
  async `do_fn`) via `inspect.iscoroutinefunction` and dispatch to a new async
  evaluation engine (`_engine.evaluate_call_async`) — same decorator, no
  `@guard_async`. `CharterInterceptor.acall()` is the async sibling of
  `.call()`. `ReversibleAction.do_fn`/`undo_fn` and a custom
  `EscalationHandler.escalate` may be `def` or `async def`; predicates
  (`pre`/`post`, `active_when`, `applies_to`) remain sync-only. See
  `examples/async_tool.py` and `CLAUDE.md`'s "Async" section. Adds
  `pytest-asyncio` as a dev dependency.
- `LICENSE` (Apache-2.0), `CONTRIBUTING.md`, `SECURITY.md`, and a GitHub
  Actions CI workflow (lint/typecheck/test across Python 3.11–3.13, then
  build).
- `pyproject.toml` now declares `license`, `classifiers`, `keywords`, and
  `[project.urls]`.
- `charter.__version__`, sourced from installed package metadata.
- `ActionLedger.dropped_count` — how many in-memory events have been evicted
  since `max_events` was reached (see Changed, below).
- `CharterInterceptor(max_sessions=...)` bounds `_step_counters` memory with
  LRU eviction (default 10,000 sessions).
- `decisions.Severity` (`Literal["high", "medium", "low"]`), replacing plain
  `str` on `RuleResult.severity`, `GuardDecision.severity`,
  `LedgerEvent.severity`, and the `severity=` parameters on `PolicySet.require()`,
  `AgentScopedPolicy`, and `@guard` — catches typos (`severity="hihg"`) under
  `mypy --strict`; no runtime behavior change.

### Changed
- **Breaking (default behavior):** `ActionLedger` now bounds its in-memory
  event list to `max_events=10_000` by default (a ring buffer — the oldest
  event is evicted once full). Pass `max_events=None` to restore the old
  unbounded behavior. `sink_path`, if configured, still captures every event
  losslessly regardless of the in-memory cap.
- A policy predicate (`pre`/`post`, `active_when`, `applies_to`) that raises
  an exception no longer crashes the tool call it was guarding. It now fails
  closed: the rule is treated as a `BLOCK` (severity `"high"`, regardless of
  the rule's own declared `on_fail`), and `active_when`/`applies_to` raising
  is treated as "policy is active" (never silently skipped). Both are logged
  via `logging.getLogger("charter.policy")`.
- `EscalationHandler.escalate()`'s `timeout_s` is now actually enforced by
  the engine (previously advisory-only): a handler that hangs or raises is
  denied within `timeout_s`, rather than blocking the tool call indefinitely.
  A sync handler that turns out to be `async def` and is used via the sync
  engine is explicitly detected and denied, rather than silently approved
  (`bool(coroutine)` is always `True`).
- If `ReversibleAction.undo()` itself raises during a post-BLOCK auto-undo,
  the ledger event for that decision is still recorded (previously, an
  exception in `undo_fn` would prevent the ledger write entirely, losing the
  audit trail for exactly the moment that most needed one).
- `CharterInterceptor(otel_tracer=...)` is now actually wired to the
  evaluation engine (previously accepted but silently ignored).
- `otel/config.py`'s global settings reassignment (`configure_otel()`/
  `reset_otel()`) is now guarded by a lock, for correctness under concurrent
  calls from multiple threads at startup.

## [0.1.0]

Initial implementation: `@guard`, `PolicySet`/`AndPolicy`/`OrPolicy`/
`NotPolicy`, `ReversibleAction`, `GuardContext`, `CharterInterceptor`
(enforce/dry_run/observe modes), `ActionLedger` (JSON/CSV export, DOT/Mermaid
policy and delegation graphs, natural-language narrative, replay),
`CharterRegistry`/`AgentScopedPolicy`/delegation helpers for multi-agent
authorization, OpenTelemetry spans and per-event metrics, a policy linter, a
ledger-driven pytest fixture generator, a synthetic-context policy REPL, and
the `charter` CLI (`report`/`lint`/`replay`/`repl`).
