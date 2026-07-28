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
