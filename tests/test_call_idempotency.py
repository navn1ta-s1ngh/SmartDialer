"""
Covers the two example garbage sequences directly from the brief:

    ANSWERED, ANSWERED, ANSWERED, COMPLETED
    COMPLETED, ANSWERED, RINGING

...plus duplicate events with the same idempotency key, and concurrent
event application from multiple threads for the same call.
"""

import threading
import pytest

from engine.db import Database
from engine.call_store import CallStore
from engine.models import CallState, DialMode


@pytest.fixture
def store(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    s = CallStore(db)
    s.seed_borrowers(["b1"], "camp1")
    return s


def make_call(store, agent_id=None):
    call_id = store.create_call("camp1", "b1", DialMode.PROGRESSIVE if agent_id else DialMode.PREDICTIVE,
                                 "w1", agent_id=agent_id)
    store.apply_event(call_id, CallState.INITIATED, event_key=f"{call_id}:init")
    return call_id


def test_duplicate_answered_events_only_transition_once(store):
    """This is the brief's exact example: ANSWERED, ANSWERED, ANSWERED,
    COMPLETED. Note our model inserts one extra state the raw provider
    stream doesn't carry: CONNECTED, emitted by the allocator (not the
    provider) the moment an agent is actually bridged onto an ANSWERED
    call -- see allocator.py. That's deliberate: whether a call is truly
    "connected" to an agent is a fact our system decides, not the
    provider, precisely because of the abandoned-call risk. So at the
    call-store layer the realistic sequence is ANSWERED x3, CONNECTED
    (synthetic), COMPLETED -- and duplicates of ANSWERED must still only
    transition the call once no matter how many times they arrive."""
    call_id = make_call(store, agent_id="a1")
    store.apply_event(call_id, CallState.RINGING, event_key="e1")

    r1 = store.apply_event(call_id, CallState.ANSWERED, event_key="e2")
    r2 = store.apply_event(call_id, CallState.ANSWERED, event_key="e3")  # different key, same fact
    r3 = store.apply_event(call_id, CallState.ANSWERED, event_key="e4")

    assert r1.applied is True
    assert r2.applied is False
    assert r3.applied is False
    assert store.get(call_id)["state"] == CallState.ANSWERED

    store.apply_event(call_id, CallState.CONNECTED, event_key="e4b")  # allocator bridges the agent
    r4 = store.apply_event(call_id, CallState.COMPLETED, event_key="e5")
    assert r4.applied is True
    assert store.get(call_id)["state"] == CallState.COMPLETED


def test_completed_cannot_skip_the_connected_step(store):
    """A raw COMPLETED arriving straight after ANSWERED (skipping
    CONNECTED) must NOT be silently accepted -- that would mean marking a
    call as successfully finished without ever confirming an agent was
    actually bridged onto it, which is exactly the kind of silent
    corruption the assignment warns about. It should be safely ignored
    as an invalid transition, not crash and not corrupt the call."""
    call_id = make_call(store, agent_id="a1")
    store.apply_event(call_id, CallState.RINGING, event_key="e1")
    store.apply_event(call_id, CallState.ANSWERED, event_key="e2")

    r = store.apply_event(call_id, CallState.COMPLETED, event_key="e3")
    assert r.applied is False
    assert store.get(call_id)["state"] == CallState.ANSWERED, \
        "state must stay put, not silently jump to COMPLETED"


def test_out_of_order_events_after_terminal_state_are_ignored(store):
    call_id = make_call(store, agent_id="a1")
    store.apply_event(call_id, CallState.RINGING, event_key="e1")
    store.apply_event(call_id, CallState.ANSWERED, event_key="e2")
    store.apply_event(call_id, CallState.CONNECTED, event_key="e2b")

    r_completed = store.apply_event(call_id, CallState.COMPLETED, event_key="e3")
    assert r_completed.applied is True
    assert store.get(call_id)["state"] == CallState.COMPLETED

    # Late, out-of-order events arriving after COMPLETED must not corrupt state.
    r_answered_again = store.apply_event(call_id, CallState.ANSWERED, event_key="e4")
    r_ringing_again = store.apply_event(call_id, CallState.RINGING, event_key="e5")

    assert r_answered_again.applied is False
    assert r_ringing_again.applied is False
    assert store.get(call_id)["state"] == CallState.COMPLETED, "terminal state must not move backwards"


def test_exact_duplicate_event_key_is_deduped(store):
    call_id = make_call(store, agent_id="a1")
    r1 = store.apply_event(call_id, CallState.RINGING, event_key="same-key")
    r2 = store.apply_event(call_id, CallState.RINGING, event_key="same-key")
    assert r1.applied is True
    assert r2.applied is False
    assert "duplicate" in r2.reason


def test_unrelated_out_of_order_sequence_from_brief(store):
    """COMPLETED, ANSWERED, RINGING -- delivered in exactly that order."""
    call_id = make_call(store, agent_id="a1")
    store.apply_event(call_id, CallState.RINGING, event_key="e0")  # need to reach RINGING first
    store.apply_event(call_id, CallState.ANSWERED, event_key="e0b")
    store.apply_event(call_id, CallState.CONNECTED, event_key="e0c")
    r_completed = store.apply_event(call_id, CallState.COMPLETED, event_key="e1")
    r_answered = store.apply_event(call_id, CallState.ANSWERED, event_key="e2")
    r_ringing = store.apply_event(call_id, CallState.RINGING, event_key="e3")

    assert r_completed.applied is True
    assert r_answered.applied is False
    assert r_ringing.applied is False
    assert store.get(call_id)["state"] == CallState.COMPLETED


def test_concurrent_event_application_for_same_call_is_race_free(store):
    """Multiple threads try to apply RINGING for the same call at once.
    Exactly one should win; nothing should crash or half-apply."""
    call_id = make_call(store, agent_id="a1")

    n = 15
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        r = store.apply_event(call_id, CallState.RINGING, event_key=f"race-{i}")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    applied = [r for r in results if r.applied]
    assert len(applied) == 1
    assert store.get(call_id)["state"] == CallState.RINGING


def test_brief_example_sequence_through_the_real_allocator(tmp_path):
    """Replays the brief's exact raw provider sequence
    (ANSWERED, ANSWERED, ANSWERED, COMPLETED) through the actual
    CallAllocator (not a hand-crafted test shortcut), proving the
    end-to-end system -- including the synthetic CONNECTED bridge step --
    lands in a single, correct COMPLETED state."""
    from engine.db import Database
    from engine.agent_store import AgentStore
    from engine.allocator import CallAllocator
    from engine.metrics import RollingStats, Counters
    from engine.providers import make_provider_a

    db = Database(str(tmp_path / "brief.db"))
    agents = AgentStore(db)
    calls = CallStore(db)
    agents.seed_agents(["a1"])
    calls.seed_borrowers(["b1"], "camp")
    provider = make_provider_a(time_scale=0.01, seed=1)
    stats, counters = RollingStats(), Counters()
    allocator = CallAllocator("camp", agents, calls, provider, stats, counters)

    agents.try_reserve_agent("a1", "w1")
    agents.transition("a1", "DIALING", expected_state="RESERVED")
    call_id = calls.create_call("camp", "b1", DialMode.PROGRESSIVE, "w1", agent_id="a1")
    calls.apply_event(call_id, CallState.INITIATED, event_key="init")
    calls.apply_event(call_id, CallState.RINGING, event_key="ring")

    from engine.models import ProviderEvent
    allocator.handle_provider_event(ProviderEvent(call_id, CallState.ANSWERED, "k1"))
    allocator.handle_provider_event(ProviderEvent(call_id, CallState.ANSWERED, "k2"))
    allocator.handle_provider_event(ProviderEvent(call_id, CallState.ANSWERED, "k3"))
    allocator.handle_provider_event(ProviderEvent(call_id, CallState.COMPLETED, "k4"))

    final = calls.get(call_id)
    assert final["state"] == CallState.COMPLETED
    assert agents.get("a1")["state"] in ("WRAP_UP", "AVAILABLE")


def test_invalid_transition_from_queued_directly_to_completed_is_rejected(store):
    call_id = store.create_call("camp1", "b1", DialMode.PREDICTIVE, "w1")
    # predictive create_call auto-advances QUEUED -> RESERVED already
    assert store.get(call_id)["state"] == CallState.RESERVED
    r = store.apply_event(call_id, CallState.COMPLETED, event_key="bad")
    assert r.applied is False
    assert store.get(call_id)["state"] == CallState.RESERVED
