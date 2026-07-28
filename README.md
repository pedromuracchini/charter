# Tollgate

[![CI](https://github.com/tollgate-dev/tollgate/actions/workflows/ci.yml/badge.svg)](https://github.com/tollgate-dev/tollgate/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tollgate.svg)](https://pypi.org/project/tollgate/)
[![Python](https://img.shields.io/pypi/pyversions/tollgate.svg)](https://pypi.org/project/tollgate/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-tollgate--dev.github.io-teal.svg)](https://tollgate-dev.github.io/tollgate/)

Tollgate expresses AI agent authorization policies as deterministic, code-defined
predicates evaluated by the runtime — never as natural-language instructions in a
prompt. Every tool call passes through the tollgate (pre-hook and post-hook)
before and after it executes. The LLM never sees, interprets, or reasons its way
around a policy: policies are plain Python, evaluated outside the model's context.

Framework-agnostic — it's a middleware layer between an agent and its tools, not a
full agent harness. See `CLAUDE.md` for architecture details and design rationale.

## Install

```bash
uv add tollgate                        # or: pip install tollgate
uv add "tollgate[otel]"                # with OpenTelemetry spans/metrics
uv add "tollgate[langgraph]"           # to wrap LangChain/LangGraph tools
uv add "tollgate[openai-agents]"       # to wrap OpenAI Agents SDK tools
uv add "tollgate[mcp]"                 # to guard MCP tools/call, client or server side
```

## Batteries included

`tollgate.policies` ships the rules nearly every agent needs, so you don't
start from a blank lambda. Each returns an ordinary `PolicySet` and composes
with `&` / `|` / `~` like anything you'd write by hand:

```python
from tollgate import TollgateInterceptor
from tollgate.policies import (
    budget_policy, domain_allowlist, no_destructive_shell,
    no_secrets_in_args, path_within, rate_limit_policy, token_budget_policy,
)

interceptor = TollgateInterceptor(policies=[
    no_secrets_in_args(),                                   # API keys, JWTs, PEM blocks
    no_destructive_shell(tool_names=("run_shell",)),        # rm -rf, mkfs, dd of=/dev/...
    path_within(["/srv/workspace"], tool_names=("write_file",)),
    domain_allowlist([".internal.corp"], tool_names=("http_get",)),
    rate_limit_policy(50),                                  # calls per session
    budget_policy(1000.0, lambda ctx: ctx.args["amount"], tool_name="transfer"),
    token_budget_policy(5.00, input_price=3.00, output_price=15.00,
                        tool_name="call_llm"),              # dollars per session
])
```

### Rate limits, budgets and token cost

Three shapes, because a limit is knowable at three different moments:

| Policy | Bounds | When the cost is known |
|---|---|---|
| `rate_limit_policy(n)` | number of calls per session | before the call |
| `budget_policy(max, amount_from=...)` | any figure derivable from the **arguments** | before the call — the cap is *never exceeded* |
| `token_budget_policy(max, input_price=…, output_price=…)` | dollars spent on model calls | only from the **response** — so the cap is *stop once spent* |
| `token_limit_policy(n)` | raw tokens per session | same as above |

An LLM call cannot be priced before it is made, so the call that crosses the
threshold completes and the *next* one is refused. That overshoot is inherent,
not a shortcut — `budget_policy(max, amount_from=..., actual_from=...)` bounds
it by charging an estimate up front and reconciling with the real figure
afterwards. Prices are per **million** tokens, the way providers quote them,
and no pricing table ships with the library: rates change, and a stale constant
baked into a security library would silently mis-bill.

`token_budget_policy` reads `usage` off the response and understands the
Anthropic (`input_tokens`), OpenAI (`prompt_tokens`) and Google
(`promptTokenCount`) shapes, as dicts or as SDK objects. A response with no
usage block contributes zero rather than failing closed. `on_fail=ESCALATE` is
often the better fit here — a human can decide whether this particular run is
worth more money.

Scope them with `tool_names` — an unscoped policy applies to every tool through
the same interceptor. The pattern matchers are seatbelts against agent
mistakes, not a sandbox: an adversary in full control of the input can evade
them.

## Quickstart

```python
from tollgate import guard, BLOCK, ESCALATE

@guard(
    pre=lambda ctx: ctx.state_checksum_matches(),
    on_fail=BLOCK,
    reason="state changed since planning",
)
def delete_record(record_id: str) -> dict:
    ...

@guard(
    pre=lambda ctx: ctx.args["amount"] < 500,
    on_fail=ESCALATE,
    escalate_to="slack://finance-approvals",
    timeout_s=300,
)
def transfer_funds(amount: float, to: str) -> dict:
    ...
```

A blocked or denied-escalation call raises `tollgate.GuardBlocked` instead of
running. Guarded functions are called with keyword arguments only — `ctx.args`
*is* those keyword arguments, mirroring how agent frameworks pass tool-call
arguments as a JSON object.

With nothing registered for the `"slack"` scheme, that `ESCALATE` above
always denies (fail-safe) — register a real handler to actually resolve it:

```python
import os

from tollgate import SlackEscalationHandler, register_handler

register_handler(
    "slack",
    SlackEscalationHandler(
        bot_token=os.environ["SLACK_BOT_TOKEN"],
        channel="C_FINANCE_APPROVALS",
        approvers={"U_CFO", "U_CONTROLLER"},  # required — see SECURITY.md
    ),
)
```

`WebhookEscalationHandler` (one synchronous POST, expects
`{"approved": bool}` back) and `CLIEscalationHandler` (local
`input()`-based human-in-the-loop) are also built in — all three use only
`urllib.request`/stdlib, no new dependency. Run
`uv run python examples/real_escalation_handlers.py` for all three,
end to end.

## Multi-agent: the same tool, different outcomes per caller

```python
from tollgate import TollgateRegistry, TollgateInterceptor, AgentScopedPolicy, BLOCK

registry = TollgateRegistry()
registry.register("clinical_agent", role="licensed_physician")
registry.register("support_agent", role="support_staff")

delete_policy = AgentScopedPolicy(
    name="only_physician_can_delete_patient",
    allowed_roles=["licensed_physician"],
    applies_to=lambda ctx: ctx.tool_name == "delete_patient",
    on_fail=BLOCK,
    reason="deleting a patient record is restricted to physicians",
)

clinical = TollgateInterceptor(registry=registry, agent_id="clinical_agent", policies=[delete_policy])
support = TollgateInterceptor(registry=registry, agent_id="support_agent", policies=[delete_policy])

clinical.call("delete_patient", delete_patient, id=1)  # allowed
support.call("delete_patient", delete_patient, id=1)   # GuardBlocked
```

Run the full worked example (registry, `ReversibleAction`, escalation, two
interceptors) with:

```bash
uv run python examples/clinical.py
```

## Async tools

`@guard` and `TollgateInterceptor.acall()` auto-detect an `async def` tool
function — no separate decorator or interceptor class needed:

```python
@guard(pre=lambda ctx: ctx.args["amount"] < 500, on_fail=BLOCK, reason="too large")
async def transfer_funds(amount: float, to: str) -> dict:
    return await payments_api.transfer(amount, to)

await transfer_funds(amount=100, to="alice")  # await it, same as the undecorated function
```

Predicates stay synchronous (cheap, deterministic checks); `ReversibleAction.do_fn`/
`undo_fn` and a custom `EscalationHandler.escalate` may be `def` or `async def`.
Run the full worked example with `uv run python examples/async_tool.py`.

## Framework integration

`interceptor.use(agent)` (or the module-level `tollgate.wrap(agent, interceptor)`)
auto-detects LangChain/LangGraph `BaseTool` objects and OpenAI Agents SDK
`FunctionTool` objects — no manual adapter registration needed:

```python
# LangGraph / LangChain — wrap the tool list before building the graph
wrapped_tools = interceptor.use([transfer_funds, search_web])
create_react_agent(model, tools=wrapped_tools)

# OpenAI Agents SDK — wrap an Agent's .tools in place
agent = Agent(name="finance_agent", tools=[transfer_funds, search_web])
interceptor.use(agent)
```

MCP is auto-detected too, on either side of the protocol:

```python
# Client side — police what your agent asks any server to do. A denial raises.
tollgate.wrap(client_session, interceptor)

# Server side — the policy holds whoever connects. A denial returns
# CallToolResult(isError=True), because raising would kill the connection.
tollgate.wrap(fastmcp_server, interceptor)
```

Requires the matching extra (`tollgate[langgraph]` / `tollgate[openai-agents]` /
`tollgate[mcp]`). Run `uv run python examples/langgraph_integration.py`,
`examples/openai_agents_integration.py`, or `examples/mcp_integration.py`.

## Reversible actions

```python
from tollgate import ReversibleAction

delete_s3_bucket = ReversibleAction(
    do_fn=lambda args: s3.delete_bucket(args["bucket"]),
    undo_fn=lambda args, snapshot: s3.restore_from_snapshot(snapshot),
    name="delete_s3_bucket",
    irreversibility_level="high",   # auto-escalates before every execution
    escalate_to="slack://infra-approvals",
    pre_snapshot=lambda args: s3.snapshot(args["bucket"]),
)
```

`irreversibility_level`: `"low"` runs normally; `"medium"` requires `undo_fn` (raised
at construction if missing); `"high"` auto-escalates before every call; `"permanent"`
is an unconditional block — the action never runs.

A `"high"` action needs an `escalate_to` target to differ from `"permanent"`:
with none, its escalation resolves to the fail-safe handler, which denies, so
every call is blocked. `tollgate lint` warns about this.

## Examples

Every file under `examples/` is runnable directly (`uv run python examples/<name>.py`):

| File | Demonstrates |
|---|---|
| `quickstart.py` | The smallest possible setup — two `@guard`-decorated functions, BLOCK and ESCALATE. |
| `policy_composition.py` | `&` / `\|` / `~` — combining `Policy` objects into a deploy-pipeline gate. |
| `dry_run_rollout.py` | Rolling out a new policy safely: `mode="dry_run"` logs what *would* block, then flip to `enforce`. |
| `custom_escalation_handler.py` | A minimal, hand-rolled `EscalationHandler` (toy in-process approve/deny logic). |
| `real_escalation_handlers.py` | The three built-in real handlers — `SlackEscalationHandler`, `WebhookEscalationHandler`, `CLIEscalationHandler` — each demonstrated end to end. |
| `reversible_levels.py` | `ReversibleAction`'s four `irreversibility_level`s side by side. |
| `delegation_chain.py` | Confused-deputy prevention via delegation-chain depth (`max_delegation_depth_policy`), not just role. |
| `clinical.py` | The full multi-agent story: `TollgateRegistry`, `AgentScopedPolicy`, `ReversibleAction`, escalation, two interceptors sharing one tool. |
| `multi_agent_orchestrator.py` | A centralized registry with 4 agents, several tools, role + trust-level policies together, and an exported delegation graph. |
| `async_tool.py` | `@guard` and `TollgateInterceptor.acall()` on `async def` tools. |
| `langgraph_integration.py` | Wrapping real `langchain_core.tools.BaseTool` objects for LangGraph (requires `tollgate[langgraph]`). |
| `openai_agents_integration.py` | Wrapping real `agents.FunctionTool` objects for the OpenAI Agents SDK (requires `tollgate[openai-agents]`). |
| `mcp_integration.py` | Guarding MCP `tools/call` from both the client and the server side (requires `tollgate[mcp]`). |
| `builtin_policies.py` | Every policy in `tollgate.policies` — secrets, destructive SQL/shell, path confinement, domain allowlist, rate limit, budget. |
| `redaction.py` | Keeping secrets and PII out of the ledger, the JSONL sink, the exports and the Slack message. |
| `audit_and_reporting.py` | `ActionLedger`'s compliance/graph/narrative/pytest-fixture export methods. |

## Audit trail

Every decision is recorded to a process-wide `ActionLedger`. In memory it's a
bounded ring buffer (10,000 events by default) — a memory bound, not a
durability story. For a full lossless history, point it at a JSONL file once at
startup, before the first guarded call:

```python
import tollgate

tollgate.configure_ledger(sink_path="/var/log/tollgate/decisions.jsonl")
```

Every event is mirrored to that file regardless of the in-memory cap, and
`tollgate report --ledger /var/log/tollgate/decisions.jsonl` reads it back.

### Redaction

Tool arguments reach the ledger, that JSONL file, the JSON/CSV exports and the
escalation message posted to Slack. **Credential-shaped values are scrubbed
out of all of them by default**, along with values under names like
`password` / `api_key` / `authorization`:

```python
interceptor.call("call_api", call_api, authorization="Bearer AKIAIOSFODNN7EXAMPLE")
# ledger: {"authorization": "[REDACTED]"}
```

Policies still evaluate against the **real** arguments — redaction happens at
record time, so a policy that inspects a credential can still see it. PII
patterns (email, SSN, IBAN, Luhn-checked card numbers) are opt-in, because an
email address is often the point of the call:

```python
tollgate.configure_redaction(include_pii=True, keys=["mrn", "dob"])
tollgate.configure_redaction(enabled=False)          # off entirely
tollgate.configure_redaction(redactor=MyDLPClient())  # your own scrubber
```

One tradeoff: `tollgate.replay()` reconstructs its context from the stored
event, so replaying a redacted call feeds placeholders to the predicates.
`ReplayResult.redacted` flags it, and `export --format fixtures` skips those
events instead of emitting tests that cannot pass. Run
`uv run python examples/redaction.py` for the whole picture.

## CLI

```bash
uv run tollgate --version
uv run tollgate report --agent my_agent.py                        # policy inventory + coverage
uv run tollgate report --agent my_agent.py --format mermaid       # coverage graph
uv run tollgate report --agent my_agent.py --delegation           # delegation graph
uv run tollgate report --agent my_agent.py --fail-under 0.8       # CI gate on tool coverage
uv run tollgate lint --agent my_agent.py                          # static policy checks
uv run tollgate replay evt_a3f9b2                                 # replay a ledger event
uv run tollgate repl --agent my_agent.py                          # synthetic-context REPL
uv run tollgate export --format narrative --ledger audit.jsonl    # plain-English audit summary
uv run tollgate export --format fixtures -o test_policies.py      # pytest tests from real decisions
```

`report --fail-under` and `lint` both exit non-zero on failure, so they work as
CI gates. `export` accepts `json`, `csv`, `narrative` and `fixtures`.

The CLI loads `my_agent.py` as a plain module and expects a module-level
`POLICIES: list[Policy]` (and, optionally, `REGISTRY` / `TOOL_NAMES` /
`ACTIONS`) — see
`examples/clinical.py`.

## Development

```bash
uv sync --extra otel --extra langgraph --extra openai-agents --extra mcp
uv run pytest -q
uv run ruff check .
uv run mypy src/tollgate
```

Commits follow [Conventional Commits](https://www.conventionalcommits.org/) and
CI enforces it — see `CONTRIBUTING.md`.

Docs are published at
[tollgate-dev.github.io/tollgate](https://tollgate-dev.github.io/tollgate/),
built from this repository's own markdown:

```bash
uv run python scripts/build_docs.py
uv run --with mkdocs-material mkdocs serve
```

See `CLAUDE.md` for what's implemented vs. deferred in this version.
