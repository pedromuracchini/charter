import sys
import threading

from tollgate.state import CallState


def test_counts_are_per_session_and_per_tool():
    state = CallState()
    state.record_call("s1", "a")
    state.record_call("s1", "a")
    state.record_call("s1", "b")
    state.record_call("s2", "a")

    assert state.call_count("s1", "a") == 2
    assert state.call_count("s1", "b") == 1
    assert state.call_count("s1", "*") == 3
    assert state.call_count("s2", "a") == 1
    assert state.call_count("unknown", "a") == 0


def test_spend_accumulates_and_is_reported_per_session():
    state = CallState()
    assert state.add_spend("s1", "usd", 10.0) == 10.0
    assert state.add_spend("s1", "usd", 5.5) == 15.5
    assert state.spend("s1", "usd") == 15.5
    assert state.spend("s1", "tokens") == 0.0
    assert state.spend("s2", "usd") == 0.0


def test_reset_session_and_clear():
    state = CallState()
    state.record_call("s1", "a")
    state.record_call("s2", "a")

    state.reset_session("s1")
    assert state.call_count("s1", "a") == 0
    assert state.call_count("s2", "a") == 1

    state.clear()
    assert state.call_count("s2", "a") == 0


def test_lru_eviction_bounds_memory():
    state = CallState(max_sessions=3)
    for i in range(5):
        state.record_call(f"s{i}", "a")

    # The two oldest were evicted; the three most recent survive.
    assert state.call_count("s0", "a") == 0
    assert state.call_count("s1", "a") == 0
    assert state.call_count("s4", "a") == 1


def test_recording_a_call_marks_the_session_recently_used():
    state = CallState(max_sessions=2)
    state.record_call("keep", "a")
    state.record_call("drop", "a")
    state.record_call("keep", "a")  # refreshes "keep"
    state.record_call("new", "a")  # evicts the LRU, which is now "drop"

    assert state.call_count("keep", "a") == 2
    assert state.call_count("drop", "a") == 0


def test_counters_are_exact_under_concurrency():
    state = CallState()
    n_threads, per_thread = 8, 200
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()
        for _ in range(per_thread):
            state.record_call("shared", "tool")
            state.add_spend("shared", "usd", 1.0)

    original = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(original)

    assert state.call_count("shared", "tool") == n_threads * per_thread
    assert state.spend("shared", "usd") == float(n_threads * per_thread)
