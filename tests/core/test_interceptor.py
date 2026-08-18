import asyncio
import inspect
import warnings

import pytest

from charter._scope import current_scope
from charter.core.interceptor import CharterInterceptor
from charter.core.policy_set import PolicySet
from charter.core.reversible import ReversibleAction
from charter.decisions import BLOCK, GuardBlocked
from charter.errors import ConfigurationWarning
from charter.ledger.ledger import ActionLedger


def _blocking_policy():
    p = PolicySet("deny_all")
    p.require(lambda ctx: False, on_fail=BLOCK, reason="always denied")
    return p


def test_enforce_mode_blocks():
    interceptor = CharterInterceptor(policies=[_blocking_policy()], mode="enforce")
    with pytest.raises(GuardBlocked):
        interceptor.call("t", lambda: {"ok": True})


def test_dry_run_mode_never_blocks_but_records():
    interceptor = CharterInterceptor(policies=[_blocking_policy()], mode="dry_run")
    result = interceptor.call("t", lambda: {"ok": True})
    assert result == {"ok": True}
    events = ActionLedger.current().events()
    assert any(e.mode == "dry_run" and e.decision == "BLOCK" for e in events)


def test_observe_mode_never_blocks_but_records():
    interceptor = CharterInterceptor(policies=[_blocking_policy()], mode="observe")
    result = interceptor.call("t", lambda: {"ok": True})
    assert result == {"ok": True}
    events = ActionLedger.current().events()
    assert any(e.mode == "observe" and e.decision == "BLOCK" for e in events)


def test_wrap_tool_routes_calls_through_interceptor():
    interceptor = CharterInterceptor(policies=[])
    wrapped = interceptor.wrap_tool("t", lambda x: x * 2)
    assert wrapped(x=21) == 42
    assert "t" in interceptor.wrapped_tools


def test_use_wraps_dict_tools_via_generic_adapter():
    interceptor = CharterInterceptor(policies=[])

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
    from charter.otel.config import otel_available, reset_otel

    if not otel_available():
        pytest.skip("requires the otel extra")

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    reset_otel()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    interceptor = CharterInterceptor(policies=[_blocking_policy()], mode="observe", otel_tracer=provider)
    interceptor.call("t", lambda: {"ok": True})

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "charter.evaluate"
    reset_otel()


def test_step_counters_evict_oldest_session_beyond_max_sessions():
    interceptor = CharterInterceptor(policies=[], max_sessions=2)
    interceptor.call("t", lambda: None, session_id="s1")
    interceptor.call("t", lambda: None, session_id="s2")
    interceptor.call("t", lambda: None, session_id="s3")

    assert list(interceptor._step_counters.keys()) == ["s2", "s3"]

    # s1 was evicted; its step_index restarts at 0 rather than continuing from 1.
    scope = interceptor._build_scope(session_id="s1", domain=None)
    assert scope.step_index == 0


async def test_acall_enforces_and_records():
    interceptor = CharterInterceptor(policies=[_blocking_policy()], mode="enforce")

    async def tool():
        return {"ok": True}

    with pytest.raises(GuardBlocked):
        await interceptor.acall("t", tool)


async def test_acall_allows_a_passing_async_tool():
    interceptor = CharterInterceptor(policies=[])

    async def tool(x):
        return x * 2

    assert await interceptor.acall("t", tool, x=21) == 42


def test_wrap_tool_auto_detects_async_and_returns_awaitable():
    interceptor = CharterInterceptor(policies=[])

    async def tool(x):
        return x * 2

    wrapped = interceptor.wrap_tool("t", tool)
    assert inspect.iscoroutinefunction(wrapped)

    async def run():
        return await wrapped(x=10)

    assert asyncio.run(run()) == 20


async def test_use_wraps_mixed_sync_and_async_tools():
    interceptor = CharterInterceptor(policies=[])

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

    interceptor = CharterInterceptor(policies=[])
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


def test_args_mapping_reaches_a_tool_that_declares_domain():
    """`domain` is an ordinary argument name for a real tool; `args={...}` is
    the escape hatch that keeps it out of the interceptor's namespace."""
    interceptor = CharterInterceptor(policies=[])

    def send(domain, body):
        return f"{body}@{domain}"

    assert interceptor.call("send", send, args={"domain": "example.com", "body": "hi"}) == "hi@example.com"


def test_args_mapping_is_what_policies_see():
    seen = {}
    policy = PolicySet("capture")
    policy.require(lambda ctx: seen.update(ctx.args) or True, on_fail=BLOCK, reason="never")
    interceptor = CharterInterceptor(policies=[policy])

    interceptor.call("send", lambda domain: domain, args={"domain": "example.com"})

    assert seen == {"domain": "example.com"}


def test_args_and_kwargs_are_merged_with_kwargs_winning():
    interceptor = CharterInterceptor(policies=[])

    def tool(a, b):
        return (a, b)

    assert interceptor.call("t", tool, args={"a": 1, "b": 2}, b=3) == (1, 3)


def test_passing_domain_for_a_tool_that_declares_it_warns():
    interceptor = CharterInterceptor(policies=[])

    def send(domain, body):
        return f"{body}@{domain}"

    with pytest.warns(ConfigurationWarning, match="domain"), pytest.raises(TypeError):
        interceptor.call("send", send, domain="example.com", body="hi")


def test_passing_session_id_for_a_tool_that_declares_it_warns():
    interceptor = CharterInterceptor(policies=[])

    def resume(session_id):
        return session_id

    with pytest.warns(ConfigurationWarning, match="session_id"), pytest.raises(TypeError):
        interceptor.call("resume", resume, session_id="s1")


def test_no_warning_when_the_tool_declares_neither_reserved_name():
    interceptor = CharterInterceptor(policies=[])

    def tool(x):
        return x

    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigurationWarning)
        assert interceptor.call("t", tool, session_id="s1", domain="example.com", x=7) == 7


def test_args_mapping_and_interceptor_domain_coexist_without_warning():
    """With `args={...}` the two namespaces are already apart: the tool gets
    its own `domain`, the scope gets the interceptor's, and there is nothing
    to warn about."""
    interceptor = CharterInterceptor(policies=[])

    def send(domain):
        return (domain, current_scope().domain)

    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigurationWarning)
        result = interceptor.call("send", send, args={"domain": "tool.example"}, domain="scope.example")

    assert result == ("tool.example", "scope.example")


def test_reversible_action_treats_reserved_names_as_interceptor_options():
    """A ReversibleAction has no parameter list to inspect, so the split is by
    contract, not by detection: **kwargs are its arguments, `session_id` and
    `domain` are the interceptor's. Guessing was tried and rejected — mixing a
    scope option with tool arguments is a correct, common pattern (see
    examples/clinical.py), and a warning that fires on correct code teaches
    people to ignore warnings."""
    action = ReversibleAction(do_fn=lambda args: args, undo_fn=None, name="send")
    interceptor = CharterInterceptor(policies=[])

    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigurationWarning)
        result = interceptor.call("send", action, domain="example.com", body="hi")

    assert result == {"body": "hi"}
    assert "domain" not in result


def test_reversible_action_reaches_its_arguments_through_args():
    action = ReversibleAction(do_fn=lambda args: args, undo_fn=None, name="send")
    interceptor = CharterInterceptor(policies=[])

    with warnings.catch_warnings():
        warnings.simplefilter("error", ConfigurationWarning)
        result = interceptor.call(
            "send", action, args={"domain": "tool.example", "body": "hi"}, session_id="s1"
        )

    assert result == {"domain": "tool.example", "body": "hi"}


def test_wrap_tool_forwards_reserved_names_as_tool_arguments():
    """An agent framework invoking a wrapped tool is passing the model's
    arguments, so a `session_id` parameter must reach the tool intact."""
    interceptor = CharterInterceptor(policies=[])

    def resume(session_id, domain):
        return f"{session_id}/{domain}"

    wrapped = interceptor.wrap_tool("resume", resume)
    assert wrapped(session_id="s1", domain="example.com") == "s1/example.com"


async def test_wrap_tool_forwards_reserved_names_for_an_async_tool():
    interceptor = CharterInterceptor(policies=[])

    async def resume(session_id):
        return session_id

    wrapped = interceptor.wrap_tool("resume", resume)
    assert await wrapped(session_id="s1") == "s1"


def test_tool_arguments_may_be_named_tool_name_and_func():
    """`tool_name`/`func` are positional-only on call(), so a tool may declare
    parameters by those names."""
    interceptor = CharterInterceptor(policies=[])

    def register(tool_name, func):
        return f"{tool_name}:{func}"

    assert interceptor.call("register", register, tool_name="double", func="fn") == "double:fn"


async def test_acall_accepts_an_args_mapping_for_a_shadowing_tool():
    interceptor = CharterInterceptor(policies=[])

    async def send(domain, session_id):
        return f"{session_id}@{domain}"

    result = await interceptor.acall("send", send, args={"domain": "example.com", "session_id": "s1"})
    assert result == "s1@example.com"


async def test_acall_warns_when_a_reserved_name_is_shadowed():
    interceptor = CharterInterceptor(policies=[])

    async def send(domain):
        return domain

    with pytest.warns(ConfigurationWarning, match="domain"), pytest.raises(TypeError):
        await interceptor.acall("send", send, domain="example.com")


def test_session_id_still_reaches_the_scope_when_not_shadowed():
    interceptor = CharterInterceptor(policies=[])

    assert interceptor.call("probe", lambda: current_scope().session_id, session_id="s1") == "s1"
    assert interceptor.call("probe", lambda: current_scope().domain, domain="example.com") == "example.com"


def test_scope_defaults_when_session_id_and_domain_are_left_unset():
    interceptor = CharterInterceptor(policies=[])

    assert interceptor.call("probe", lambda: current_scope().session_id) == "default"
    assert interceptor.call("probe", lambda: current_scope().domain) is None


def test_per_interceptor_ledgers_stay_separate_from_each_other_and_the_global_one():
    ledger_a, ledger_b = ActionLedger(), ActionLedger()
    a = CharterInterceptor(policies=[_blocking_policy()], mode="observe", ledger=ledger_a)
    b = CharterInterceptor(policies=[_blocking_policy()], mode="observe", ledger=ledger_b)

    a.call("tool_a", lambda: None)
    b.call("tool_b", lambda: None)

    assert [e.tool for e in ledger_a.events()] == ["tool_a"]
    assert [e.tool for e in ledger_b.events()] == ["tool_b"]
    assert ActionLedger.current().events() == []


def test_interceptor_ledger_records_tool_errors_too():
    ledger = ActionLedger()
    interceptor = CharterInterceptor(policies=[], ledger=ledger)

    def broken():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        interceptor.call("broken", broken)

    assert [(e.decision, e.hook) for e in ledger.events()] == [("ERROR", "invoke")]
    assert ActionLedger.current().events() == []


class _KeyErasingRedactor:
    """Scrubs every argument wholesale, so a test can tell it apart from the
    process-wide default redactor."""

    def redact_args(self, args):
        return dict.fromkeys(args, "<GONE>")

    def redact_text(self, text):
        return text.replace("secret", "<GONE>")


def test_interceptor_redactor_is_what_scrubs_the_recorded_args():
    ledger = ActionLedger()
    policy = PolicySet("deny")
    policy.require(lambda ctx: False, on_fail=BLOCK, reason="the secret is bad")
    interceptor = CharterInterceptor(
        policies=[policy], mode="observe", ledger=ledger, redactor=_KeyErasingRedactor()
    )

    interceptor.call("t", lambda token: None, args={"token": "ordinary-looking"})

    event = ledger.events()[-1]
    assert event.args == {"token": "<GONE>"}
    assert event.reason == "the <GONE> is bad"


async def test_interceptor_redactor_and_ledger_apply_to_acall_too():
    ledger = ActionLedger()
    interceptor = CharterInterceptor(policies=[], ledger=ledger, redactor=_KeyErasingRedactor())

    async def broken(token):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await interceptor.acall("broken", broken, args={"token": "x"})

    assert ledger.events()[-1].args == {"token": "<GONE>"}


def test_wrapped_tools_registry_is_safe_under_concurrent_wrapping():
    import threading

    interceptor = CharterInterceptor(policies=[])
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
