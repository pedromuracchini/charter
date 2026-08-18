"""Async tool support: `@guard` and `CharterInterceptor.acall()` both
auto-detect an `async def` tool function and dispatch to the async engine —
no separate decorator or interceptor class needed.

Run directly:

    uv run python examples/async_tool.py
"""

from __future__ import annotations

import asyncio

from charter import BLOCK, CharterInterceptor, GuardBlocked, guard


@guard(pre=lambda ctx: ctx.args["amount"] < 500, on_fail=BLOCK, reason="amount too large")
async def transfer_funds(amount: float, to: str) -> dict:
    # Simulates an async I/O call (e.g. a real payments API).
    await asyncio.sleep(0.01)
    return {"transferred": amount, "to": to}


async def read_balance(account_id: str) -> dict:
    await asyncio.sleep(0.01)
    return {"account_id": account_id, "balance": 1000}


async def main() -> None:
    # @guard: works with `await` exactly like the underlying async function would.
    print(await transfer_funds(amount=100, to="alice"))
    try:
        await transfer_funds(amount=1000, to="bob")
    except GuardBlocked as exc:
        print(f"blocked: {exc.decision.reason}")

    # CharterInterceptor.acall(): the interceptor-based equivalent, useful when
    # wiring up an agent's full toolset (mode=enforce/dry_run/observe, identity, ...).
    interceptor = CharterInterceptor(policies=[])
    print(await interceptor.acall("read_balance", read_balance, account_id="acct_1"))


if __name__ == "__main__":
    asyncio.run(main())
