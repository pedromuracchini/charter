# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) once it
reaches `1.0.0`. Before `1.0.0`, minor versions may include breaking changes.

## [Unreleased]

Production-readiness hardening pass, following an adoption audit of the
initial implementation.

### Fixed
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
  `TollgateInterceptor` now appends its own `agent_id` when building the
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
- **`TollgateInterceptor` was not thread-safe.** `_build_scope()`
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
  existed on `LedgerEvent`, the `tollgate.policy_hash` span attribute was
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
  `tollgate.configure_ledger`, plus a read-only `sink_path` property.
  `current()` is now created under a lock (two threads racing on the first call
  each built a ledger, and one set of events was silently lost).
- **`ReversibleAction(irreversibility_level="high")` could never escalate.**
  Its intrinsic `RuleResult` was built with `escalate_to=None`, which
  `resolve_handler` maps to the fail-safe denier — so `"high"` was in practice
  a synonym for `"permanent"`, contradicting the documented "auto-escalates
  before every execution". `ReversibleAction` now takes `escalate_to` and
  `timeout_s` and propagates both into the intrinsic check. `tollgate lint`
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
- **CLI: `--version`, `report --fail-under`, and an `export` command.**
  `report` always exited 0, so it was useless as a CI gate; `--fail-under
  RATIO` now exits non-zero when tool coverage falls below the threshold (the
  report is still printed, so CI logs show what failed).
  `tollgate export --format json|csv|narrative|fixtures [--output PATH]`
  reaches `export_compliance_report`, `narrative()` and
  `fixtures_from_events()`, none of which had a CLI command before. `lint`
  takes `--agent` like every other subcommand, still accepting its historical
  positional form.
- **`tollgate.policies` — a library of ready-made policies.** Previously every
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
  (`tollgate.state`), backing the rate-limit and budget policies that were
  previously listed as deferred. Lives beside `GuardContext` rather than in
  it: a lock-guarded, LRU-bounded object injected into the `ExecutionScope`
  like the existing checksum/consent providers, read through
  `ctx.calls_this_session()` / `ctx.spent()` / `ctx.record_spend()`. Replay
  reconstructs a scope without one, so history-dependent policies report zero
  instead of reading live counters. `TollgateInterceptor` gained a
  `call_state` parameter — pass a shared instance to enforce one quota across
  several agents.
- **MCP adapter** (`tollgate[mcp]`), guarding `tools/call` on both sides of
  the protocol: `guard_mcp_session` wraps a `ClientSession` (a denial raises
  `GuardBlocked`), `guard_mcp_server` wraps a `FastMCP`/low-level `Server` (a
  denial returns `CallToolResult(isError=True)`, since an exception escaping a
  request handler would tear down the connection for every later request).
  Auto-detected by `interceptor.use()`/`tollgate.wrap()`. New example
  `examples/mcp_integration.py` and tests against real MCP objects over an
  in-memory transport.
- **Escalation metrics**, which did not exist at all: `tollgate.escalations_total`
  (attributed with `outcome` — `approved` / `denied` / `not_resolved` — plus
  policy, tool and `escalate_to`) and `tollgate.escalation_latency_ms`. An
  escalation is the one decision that puts a human in the request path, so its
  rate, approve/deny split and wait time are the highest-value operational
  signals Tollgate can emit.
- **`tollgate.delegation_depth` is now actually emitted.**
  `record_delegation_depth()` existed and was documented but no call site ever
  invoked it. The engine now records it per call, attributed to the calling
  agent.
- **Richer OTEL spans**: `tollgate.tool` (previously absent — spans could not
  be grouped by tool in a backend), `tollgate.session_id`,
  `tollgate.step_index`, `tollgate.trust_level`, and an ERROR span status on
  an enforced BLOCK so denials surface in a trace UI's error views (not set in
  `dry_run`/`observe`, where nothing was actually denied).
- **Real `EscalationHandler` implementations** under `tollgate.escalation`:
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
  cleanly interrupted mid-call). Exported from `tollgate/__init__.py`. New
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
  **registered by default** (`src/tollgate/adapters/__init__.py`, new) —
  `interceptor.use(agent)`/`tollgate.wrap(agent, interceptor)` now auto-detect
  them, no manual `register_adapter()` call needed. New optional dependency
  groups `tollgate[langgraph]` and `tollgate[openai-agents]`. New examples
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
- **Async support.** `@guard` and `TollgateInterceptor.wrap_tool()`/`.use()`
  auto-detect an `async def` tool function (or a `ReversibleAction` with an
  async `do_fn`) via `inspect.iscoroutinefunction` and dispatch to a new async
  evaluation engine (`_engine.evaluate_call_async`) — same decorator, no
  `@guard_async`. `TollgateInterceptor.acall()` is the async sibling of
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
- `tollgate.__version__`, sourced from installed package metadata.
- `ActionLedger.dropped_count` — how many in-memory events have been evicted
  since `max_events` was reached (see Changed, below).
- `TollgateInterceptor(max_sessions=...)` bounds `_step_counters` memory with
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
  via `logging.getLogger("tollgate.policy")`.
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
- `TollgateInterceptor(otel_tracer=...)` is now actually wired to the
  evaluation engine (previously accepted but silently ignored).
- `otel/config.py`'s global settings reassignment (`configure_otel()`/
  `reset_otel()`) is now guarded by a lock, for correctness under concurrent
  calls from multiple threads at startup.

## [0.1.0]

Initial implementation: `@guard`, `PolicySet`/`AndPolicy`/`OrPolicy`/
`NotPolicy`, `ReversibleAction`, `GuardContext`, `TollgateInterceptor`
(enforce/dry_run/observe modes), `ActionLedger` (JSON/CSV export, DOT/Mermaid
policy and delegation graphs, natural-language narrative, replay),
`TollgateRegistry`/`AgentScopedPolicy`/delegation helpers for multi-agent
authorization, OpenTelemetry spans and per-event metrics, a policy linter, a
ledger-driven pytest fixture generator, a synthetic-context policy REPL, and
the `tollgate` CLI (`report`/`lint`/`replay`/`repl`).
