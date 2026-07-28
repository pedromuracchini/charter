# Tollgate

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
```

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

Requires the matching extra (`tollgate[langgraph]` / `tollgate[openai-agents]`).
Run `uv run python examples/langgraph_integration.py` or
`uv run python examples/openai_agents_integration.py`.

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
| `audit_and_reporting.py` | `ActionLedger`'s compliance/graph/narrative/pytest-fixture export methods. |

## CLI

```bash
uv run tollgate report --agent my_agent.py                       # policy inventory + coverage
uv run tollgate report --agent my_agent.py --format mermaid       # coverage graph
uv run tollgate report --agent my_agent.py --delegation           # delegation graph
uv run tollgate lint my_agent.py                                  # static policy checks
uv run tollgate replay evt_a3f9b2                                 # replay a ledger event
uv run tollgate repl --agent my_agent.py                          # synthetic-context REPL
```

The CLI loads `my_agent.py` as a plain module and expects a module-level
`POLICIES: list[Policy]` (and, optionally, `REGISTRY` / `TOOL_NAMES` /
`ACTIONS`) — see
`examples/clinical.py`.

## Development

```bash
uv sync --extra otel
uv run pytest -q
uv run ruff check .
uv run mypy src/tollgate
```

See `CLAUDE.md` for what's implemented vs. deferred in this version.
