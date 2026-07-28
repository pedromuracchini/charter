import asyncio

import pytest

from tollgate._scope import current_scope
from tollgate.core.interceptor import TollgateInterceptor
from tollgate.core.policy_set import PolicySet
from tollgate.decisions import BLOCK, GuardBlocked
from tollgate.ledger.ledger import ActionLedger


def _blocking_policy():
    p = PolicySet("deny_all")
    p.require(lambda ctx: False, on_fail=BLOCK, reason="always denied")
    return p


def test_enforce_mode_blocks():
    interceptor = TollgateInterceptor(policies=[_blocking_policy()], mode="enforce")
    with pytest.raises(GuardBlocked):
        interceptor.call("t", lambda: {"ok": True})


def test_dry_run_mode_never_blocks_but_records():
    interceptor = TollgateInterceptor(policies=[_blocking_policy()], mode="dry_run")
    result = interceptor.call("t", lambda: {"ok": True})
    assert result == {"ok": True}
    events = ActionLedger.current().events()
    assert any(e.mode == "dry_run" and e.decision == "BLOCK" for e in events)


def test_observe_mode_never_blocks_but_records():
    interceptor = TollgateInterceptor(policies=[_blocking_policy()], mode="observe")
    result = interceptor.call("t", lambda: {"ok": True})
    assert result == {"ok": True}
    events = ActionLedger.current().events()
    assert any(e.mode == "observe" and e.decision == "BLOCK" for e in events)


def test_wrap_tool_routes_calls_through_interceptor():
    interceptor = TollgateInterceptor(policies=[])
    wrapped = interceptor.wrap_tool("t", lambda x: x * 2)
    assert wrapped(x=21) == 42
    assert "t" in interceptor.wrapped_tools


def test_use_wraps_dict_tools_via_generic_adapter():
    interceptor = TollgateInterceptor(policies=[])

    class Agent:
        def __init__(self):
            self.tools = {"double": lambda x: x * 2}

    agent = Agent()
    interceptor.use(agent)
    assert agent.tools["double"](x=10) == 20


def test_otel_tracer_is_honored_without_configure_otel():
    """An interceptor's own `otel_tracer` must emit spans to that provider even
    if the global `configure_otel()` was never called — see CLAUDE.md's
    Sampling/OTEL notes; this used to be a silently-ignored parameter."""
    from tollgate.otel.config import otel_available, reset_otel

    if not otel_available():
        return

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    reset_otel()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    interceptor = TollgateInterceptor(policies=[_blocking_policy()], mode="observe", otel_tracer=provider)
    interceptor.call("t", lambda: {"ok": True})

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "tollgate.evaluate"
    reset_otel()


def test_step_counters_evict_oldest_session_beyond_max_sessions():
    interceptor = TollgateInterceptor(policies=[], max_sessions=2)
    interceptor.call("t", lambda: None, session_id="s1")
    interceptor.call("t", lambda: None, session_id="s2")
    interceptor.call("t", lambda: None, session_id="s3")

    assert list(interceptor._step_counters.keys()) == ["s2", "s3"]

    # s1 was evicted; its step_index restarts at 0 rather than continuing from 1.
    scope = interceptor._build_scope(session_id="s1", domain=None)
    assert scope.step_index == 0


async def test_acall_enforces_and_records():
    interceptor = TollgateInterceptor(policies=[_blocking_policy()], mode="enforce")

    async def tool():
        return {"ok": True}

    with pytest.raises(GuardBlocked):
        await interceptor.acall("t", tool)


async def test_acall_allows_a_passing_async_tool():
    interceptor = TollgateInterceptor(policies=[])

    async def tool(x):
        return x * 2

    assert await interceptor.acall("t", tool, x=21) == 42


def test_wrap_tool_auto_detects_async_and_returns_awaitable():
    interceptor = TollgateInterceptor(policies=[])

    async def tool(x):
        return x * 2

    wrapped = interceptor.wrap_tool("t", tool)
    assert asyncio.iscoroutinefunction(wrapped)

    async def run():
        return await wrapped(x=10)

    assert asyncio.run(run()) == 20


async def test_use_wraps_mixed_sync_and_async_tools():
    interceptor = TollgateInterceptor(policies=[])

    async def async_double(x):
        return x * 2

    class Agent:
        def __init__(self):
            self.tools = {"double": lambda x: x * 2, "async_double": async_double}

    agent = Agent()
    interceptor.use(agent)
    assert agent.tools["double"](x=10) == 20
    assert await agent.tools["async_double"](x=10) == 20


def test_step_index_is_unique_under_concurrent_calls():
    """One interceptor shared across request threads is the normal server
    shape, and _build_scope() read-modify-writes _step_counters. Unlocked, two
    threads could read the same value and emit a duplicate step_index."""
    import sys
    import threading

    interceptor = TollgateInterceptor(policies=[])
    seen: list[int] = []
    seen_lock = threading.Lock()
    n_threads, per_thread = 8, 60
    barrier = threading.Barrier(n_threads)

    def record_step():
        return current_scope().step_index

    def worker():
        barrier.wait()
        for _ in range(per_thread):
            step = interceptor.call("probe", record_step, session_id="shared")
            with seen_lock:
                seen.append(step)

    # The read-modify-write is only a handful of bytecodes wide, so at the
    # default 5ms switch interval the GIL almost never preempts inside it and
    # the race stays invisible. Shrinking the interval makes preemption
    # routine — without the lock this assertion fails reliably.
    original_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(original_interval)

    assert sorted(seen) == list(range(n_threads * per_thread))


def test_wrapped_tools_registry_is_safe_under_concurrent_wrapping():
    import threading

    interceptor = TollgateInterceptor(policies=[])
    barrier = threading.Barrier(8)

    def worker(index: int):
        barrier.wait()
        for i in range(20):
            interceptor.wrap_tool(f"tool_{index}_{i}", lambda **kw: None)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(interceptor.wrapped_tools) == 8 * 20
