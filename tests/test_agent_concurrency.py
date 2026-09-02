"""
The single most important test in this assignment:

    "I want a test where multiple workers simultaneously attempt to
    reserve the same agent and prove that only one succeeds."

We use a threading.Barrier so every "worker" thread calls
try_reserve_agent for the SAME agent_id at, as close as the OS scheduler
allows, the same instant -- this is a genuine race, not a simulated one.
"""

import os
import threading
import pytest

from engine.db import Database
from engine.agent_store import AgentStore
from engine.models import AgentState


@pytest.fixture
def store(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    s = AgentStore(db)
    s.seed_agents(["agent-1"])
    return s


def test_only_one_worker_wins_the_race(store):
    N_WORKERS = 25
    barrier = threading.Barrier(N_WORKERS)
    results = [None] * N_WORKERS

    def worker(i):
        barrier.wait()  # line everyone up, then release simultaneously
        results[i] = store.try_reserve_agent("agent-1", f"worker-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    assert len(successes) == 1, (
        f"expected exactly 1 winner, got {len(successes)}: {successes}"
    )
    assert len(failures) == N_WORKERS - 1

    row = store.get("agent-1")
    assert row["state"] == AgentState.RESERVED
    assert row["worker_id"] == successes[0].worker_id


def test_race_repeated_many_times_stays_correct(tmp_path):
    """Run the race 30 times in a row on fresh agents to make sure the
    single-winner property isn't a fluke of one particular interleaving."""
    db = Database(str(tmp_path / "test2.db"))
    store = AgentStore(db)
    N_WORKERS = 10
    N_ROUNDS = 30
    agent_ids = [f"agent-{r}" for r in range(N_ROUNDS)]
    store.seed_agents(agent_ids)

    for r in range(N_ROUNDS):
        agent_id = agent_ids[r]
        barrier = threading.Barrier(N_WORKERS)
        results = []
        lock = threading.Lock()

        def worker(i):
            barrier.wait()
            res = store.try_reserve_agent(agent_id, f"w{i}")
            with lock:
                results.append(res)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_WORKERS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        successes = [x for x in results if x.success]
        assert len(successes) == 1, f"round {r}: expected 1 winner, got {len(successes)}"


def test_try_reserve_any_available_never_double_allocates(tmp_path):
    """Many workers pull from the shared AVAILABLE pool concurrently;
    every agent should end up reserved by exactly one worker, and no
    worker should get None while agents are still free."""
    db = Database(str(tmp_path / "test3.db"))
    store = AgentStore(db)
    n_agents = 15
    store.seed_agents([f"a{i}" for i in range(n_agents)])

    barrier = threading.Barrier(n_agents)
    results = []
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        agent_id = store.try_reserve_any_available(f"worker-{i}")
        with lock:
            results.append(agent_id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_agents)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    won = [r for r in results if r is not None]
    assert len(won) == n_agents, "every agent should have been claimed exactly once"
    assert len(set(won)) == n_agents, "no agent_id should appear twice -- that would mean a double allocation"


def test_stale_reservation_is_reclaimed_after_lease_expiry(store):
    """Simulates a worker crash: it reserves an agent and never comes
    back. Once the lease expires, the reaper must reclaim the agent so
    it doesn't leak forever."""
    import time
    res = store.try_reserve_agent("agent-1", "doomed-worker", lease_seconds=0.05)
    assert res.success
    assert store.get("agent-1")["state"] == AgentState.RESERVED

    # not yet expired -> nothing to reclaim
    assert store.reclaim_stale_reservations() == 0

    time.sleep(0.1)
    reclaimed = store.reclaim_stale_reservations()
    assert reclaimed == 1
    row = store.get("agent-1")
    assert row["state"] == AgentState.AVAILABLE
    assert row["worker_id"] is None


def test_borrower_claim_is_also_race_free(tmp_path):
    from engine.call_store import CallStore
    db = Database(str(tmp_path / "test4.db"))
    calls = CallStore(db)
    calls.seed_borrowers(["b1"], "camp1")

    n_workers = 12
    barrier = threading.Barrier(n_workers)
    results = []
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        r = calls.try_claim_borrower("camp1", f"w{i}")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    won = [r for r in results if r is not None]
    assert len(won) == 1, "exactly one worker should claim the single borrower"
