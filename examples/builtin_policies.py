"""The `charter.policies` library: the rules every agent needs, prebuilt.

Each one is an ordinary `PolicySet`, so they compose with `&`/`|`/`~` and mix
freely with policies you write yourself. Scope them with `tool_names` — a
policy with no scope applies to *every* tool through the same interceptor.

Run directly:

    uv run python examples/builtin_policies.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from charter import CharterInterceptor, GuardBlocked
from charter.policies import (
    budget_policy,
    domain_allowlist,
    no_destructive_shell,
    no_destructive_sql,
    no_secrets_in_args,
    path_within,
    rate_limit_policy,
    token_budget_policy,
)

WORKSPACE = Path(tempfile.mkdtemp(prefix="charter-workspace-"))


# --- the tools. None of them knows Charter exists. ------------------------


def run_sql(query: str) -> dict:
    return {"rows": 0, "query": query}


def run_shell(command: str) -> dict:
    return {"exit_code": 0, "command": command}


def write_file(path: str, body: str) -> dict:
    return {"written": path}


def http_post(url: str, body: str) -> dict:
    return {"status": 200, "url": url}


def transfer(amount: float, to: str) -> dict:
    return {"transferred": amount, "to": to}


def search(query: str) -> dict:
    return {"results": [query]}


def call_llm(prompt: str) -> dict:
    """Shaped like a real model response — the usage block is what gets priced."""
    return {"text": "...", "usage": {"input_tokens": 200_000, "output_tokens": 20_000}}


def build_interceptor() -> CharterInterceptor:
    return CharterInterceptor(
        agent_id="ops_agent",
        policies=[
            # Credentials must never leave in a tool argument. Unscoped on
            # purpose: this one genuinely should apply to every tool.
            no_secrets_in_args(),
            no_destructive_sql(tool_names=("run_sql",)),
            no_destructive_shell(tool_names=("run_shell",)),
            # Resolves before comparing, so `..` and symlinks are caught too.
            path_within([WORKSPACE], tool_names=("write_file",)),
            # Matches the parsed hostname, so `api.stripe.com.evil.com` fails.
            domain_allowlist(["api.stripe.com"], tool_names=("http_post",)),
            rate_limit_policy(3, tool_name="search"),
            # Cost is in the arguments, so the cap is never exceeded.
            budget_policy(100.0, lambda ctx: ctx.args["amount"], tool_name="transfer"),
            # Cost is only in the *response*, so the cap is "stop once spent".
            # Prices are per million tokens, the way providers quote them.
            token_budget_policy(2.00, input_price=3.00, output_price=15.00, tool_name="call_llm"),
        ],
    )


def attempt(interceptor: CharterInterceptor, label: str, tool, **kwargs) -> None:
    try:
        interceptor.call(tool.__name__, tool, **kwargs)
        print(f"  allowed  {label}")
    except GuardBlocked as exc:
        print(f"  BLOCKED  {label}\n             -> {exc.decision.reason}")


def main() -> None:
    interceptor = build_interceptor()

    print("\n=== secrets in arguments ===")
    attempt(
        interceptor,
        "http_post with an ordinary body",
        http_post,
        url="https://api.stripe.com/v1",
        body="hello",
    )
    attempt(
        interceptor,
        "http_post leaking an AWS key",
        http_post,
        url="https://api.stripe.com/v1",
        body="key=AKIAIOSFODNN7EXAMPLE",
    )

    print("\n=== destructive SQL ===")
    attempt(interceptor, "SELECT", run_sql, query="SELECT * FROM users WHERE id = 1")
    attempt(interceptor, "DELETE with a WHERE clause", run_sql, query="DELETE FROM users WHERE id = 1")
    attempt(interceptor, "DELETE with no WHERE clause", run_sql, query="DELETE FROM users")
    attempt(interceptor, "DROP TABLE", run_sql, query="DROP TABLE users")

    print("\n=== destructive shell ===")
    attempt(interceptor, "ls", run_shell, command="ls -la /srv")
    attempt(interceptor, "rm -rf", run_shell, command="rm -rf /srv/data")

    print("\n=== path confinement ===")
    attempt(interceptor, "inside the workspace", write_file, path=str(WORKSPACE / "notes.md"), body="x")
    attempt(interceptor, "traversal out of it", write_file, path=str(WORKSPACE / ".." / "escape"), body="x")

    print("\n=== domain allowlist ===")
    attempt(interceptor, "allowlisted host", http_post, url="https://api.stripe.com/v1/charges", body="{}")
    attempt(
        interceptor,
        "lookalike host (substring check would pass this)",
        http_post,
        url="https://api.stripe.com.evil.example/collect",
        body="{}",
    )
    attempt(interceptor, "plain http", http_post, url="http://api.stripe.com/v1", body="{}")

    print("\n=== rate limit (3 per session) ===")
    for i in range(1, 5):
        attempt(interceptor, f"search #{i}", search, query="charter")

    print("\n=== budget (100 per session) ===")
    attempt(interceptor, "transfer 60", transfer, amount=60.0, to="alice")
    attempt(interceptor, "transfer 30 (total 90)", transfer, amount=30.0, to="bob")
    attempt(interceptor, "transfer 20 (would exceed)", transfer, amount=20.0, to="carol")
    attempt(interceptor, "transfer 10 (still fits)", transfer, amount=10.0, to="dave")

    print("\n=== LLM token budget ($2, ~$0.90 per call) ===")
    for i in range(1, 5):
        attempt(interceptor, f"call_llm #{i}", call_llm, prompt="summarize this")
    print("  ^ the 3rd call crossed the cap and still ran: an LLM call cannot be")
    print("    priced before it is made, so this is 'stop once spent', not 'never exceed'.")

    print("\n=== a fresh session starts clean ===")
    interceptor.call("search", search, session_id="another", query="charter")
    print("  allowed  search in a different session, despite the first being exhausted")


if __name__ == "__main__":
    main()
