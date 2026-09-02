"""
Agent store: the single source of truth for agent state.

THE CORE GUARANTEE THIS FILE PROVIDES
----------------------------------------
Two workers must never be able to reserve the same agent concurrently.

We get this NOT with a Python threading.Lock (which only protects threads
inside one process -- useless once you have multiple worker processes or
machines) but with a single atomic conditional UPDATE:

    UPDATE agents SET state='RESERVED', ...
    WHERE agent_id=? AND state='AVAILABLE'

SQLite serializes all writer transactions against the same database file,
so exactly one of two simultaneous UPDATEs for the same agent_id can see
state='AVAILABLE' and match the WHERE clause; the other UPDATE affects
zero rows. We check `cursor.rowcount` to find out which one we were. This
is the same "compare-and-set via WHERE clause" pattern you would use
against Postgres with `SELECT ... FOR UPDATE` or an optimistic version
column -- it generalizes to real multi-process workers, not just threads.
"""

from __future__ import annotations
import time
from typing import Optional

from .models import AgentState, AGENT_TRANSITIONS, ReservationResult
from .db import Database


class AgentStore:
    def __init__(self, db: Database):
        self.db = db

    # -- setup -------------------------------------------------------------

    def seed_agents(self, agent_ids):
        now = time.time()
        conn = self.db.conn()
        for aid in agent_ids:
            conn.execute(
                "INSERT OR REPLACE INTO agents(agent_id, state, worker_id, call_id, "
                "lease_expires_at, wrap_until, version, updated_at) "
                "VALUES (?, ?, NULL, NULL, NULL, NULL, 0, ?)",
                (aid, AgentState.AVAILABLE, now),
            )

    # -- the critical section ------------------------------------------------

    def try_reserve_agent(self, agent_id: str, worker_id: str,
                           lease_seconds: float = 15.0) -> ReservationResult:
        """Atomically claim one specific agent. success=False means someone
        else got there first, or the agent isn't AVAILABLE."""
        now = time.time()
        lease_expires_at = now + lease_seconds
        cur = self.db.execute_with_retry(
            "UPDATE agents SET state=?, worker_id=?, lease_expires_at=?, "
            "version=version+1, updated_at=? "
            "WHERE agent_id=? AND state=?",
            (AgentState.RESERVED, worker_id, lease_expires_at, now,
             agent_id, AgentState.AVAILABLE),
        )
        if cur.rowcount == 1:
            return ReservationResult(True, agent_id, worker_id, "reserved")
        return ReservationResult(False, agent_id, worker_id,
                                  "lost race or agent not available")

    def try_connect_any_available(self, call_id: str, max_attempts: int = 5) -> Optional[str]:
        """Predictive-mode 'instant bind': atomically move a free agent
        straight from AVAILABLE to CONNECTED in a single UPDATE, for the
        moment a borrower answers a call that had no agent pre-bound.
        Deliberately a separate atomic statement from try_reserve_agent
        (which targets RESERVED) rather than reusing it + a second
        transition -- two separate atomic statements would leave a window
        where a concurrent reclaim could observe the agent sitting in
        RESERVED with no owner in between."""
        conn = self.db.conn()
        for _ in range(max_attempts):
            row = conn.execute(
                "SELECT agent_id FROM agents WHERE state=? ORDER BY updated_at LIMIT 1",
                (AgentState.AVAILABLE,),
            ).fetchone()
            if row is None:
                return None
            agent_id = row["agent_id"]
            now = time.time()
            cur = self.db.execute_with_retry(
                "UPDATE agents SET state=?, call_id=?, worker_id=NULL, lease_expires_at=NULL, "
                "version=version+1, updated_at=? WHERE agent_id=? AND state=?",
                (AgentState.CONNECTED, call_id, now, agent_id, AgentState.AVAILABLE),
            )
            if cur.rowcount == 1:
                return agent_id
        return None

    def try_reserve_any_available(self, worker_id: str, lease_seconds: float = 15.0,
                                   max_attempts: Optional[int] = None) -> Optional[str]:
        """Pick a candidate AVAILABLE agent and try to reserve it. If we
        lose the race (someone else grabbed it between our SELECT and our
        UPDATE) we just try the next candidate. This is the standard
        'optimistic concurrency + retry' pattern -- cheap because
        contention on any *single* agent row is rare even when contention
        on the *pool* is high.

        `ORDER BY updated_at LIMIT 1` is deterministic, so every caller
        racing for the pool targets the same candidate first: the loser of
        each round retries against the next-oldest row, not a random one.
        Under high fan-in (N workers racing for a small pool) a given
        worker can legitimately lose several rounds in a row before the
        pool empties. A fixed retry cap (the previous default of 5) can
        therefore give up and return None while AVAILABLE agents still
        exist -- so this only stops when the SELECT itself finds no
        AVAILABLE row left (or an explicit `max_attempts` override is hit),
        never on an arbitrary round count."""
        conn = self.db.conn()
        attempts = 0
        while max_attempts is None or attempts < max_attempts:
            attempts += 1
            row = conn.execute(
                "SELECT agent_id FROM agents WHERE state=? ORDER BY updated_at LIMIT 1",
                (AgentState.AVAILABLE,),
            ).fetchone()
            if row is None:
                return None
            result = self.try_reserve_agent(row["agent_id"], worker_id, lease_seconds)
            if result.success:
                return result.agent_id
        return None

    # -- explicit, validated transitions -------------------------------------

    def transition(self, agent_id: str, new_state: str, *, expected_state: Optional[str] = None,
                    call_id: Optional[str] = None, clear_worker: bool = False,
                    wrap_seconds: Optional[float] = None) -> bool:
        """Generic validated transition. Always goes through
        AGENT_TRANSITIONS so an illegal jump (e.g. OFFLINE -> CONNECTED)
        is impossible even by accident elsewhere in the codebase."""
        conn = self.db.conn()
        row = conn.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        if row is None:
            return False
        current = row["state"]
        if expected_state is not None and current != expected_state:
            return False
        if new_state not in AGENT_TRANSITIONS.get(current, set()):
            return False

        now = time.time()
        set_clauses = ["state=?", "version=version+1", "updated_at=?"]
        params = [new_state, now]
        if call_id is not None:
            set_clauses.append("call_id=?")
            params.append(call_id)
        if new_state in (AgentState.AVAILABLE, AgentState.OFFLINE):
            set_clauses += ["call_id=NULL", "lease_expires_at=NULL", "wrap_until=NULL"]
        if clear_worker:
            set_clauses.append("worker_id=NULL")
        if wrap_seconds is not None:
            set_clauses.append("wrap_until=?")
            params.append(now + wrap_seconds)

        params += [agent_id, current]
        cur = self.db.execute_with_retry(
            f"UPDATE agents SET {', '.join(set_clauses)} WHERE agent_id=? AND state=?",
            tuple(params),
        )
        return cur.rowcount == 1

    def release_to_available(self, agent_id: str) -> bool:
        """Best-effort release from whatever state the agent is currently
        in (used for cascading failures) -- tries the legal path back to
        AVAILABLE rather than assuming one specific prior state."""
        conn = self.db.conn()
        row = conn.execute("SELECT state FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        if row is None:
            return False
        current = row["state"]
        if current == AgentState.AVAILABLE:
            return True
        if AgentState.AVAILABLE in AGENT_TRANSITIONS.get(current, set()):
            return self.transition(agent_id, AgentState.AVAILABLE, expected_state=current,
                                    clear_worker=True)
        return False

    def set_offline(self, agent_id: str) -> Optional[str]:
        """Force an agent OFFLINE (simulating disappearance / logout).
        Returns the call_id that was bound to it, if any, so the caller
        can cascade-fail that call instead of leaking it silently."""
        conn = self.db.conn()
        row = conn.execute("SELECT state, call_id FROM agents WHERE agent_id=?",
                            (agent_id,)).fetchone()
        if row is None:
            return None
        bound_call = row["call_id"]
        now = time.time()
        self.db.execute_with_retry(
            "UPDATE agents SET state=?, call_id=NULL, worker_id=NULL, "
            "lease_expires_at=NULL, wrap_until=NULL, version=version+1, updated_at=? "
            "WHERE agent_id=?",
            (AgentState.OFFLINE, now, agent_id),
        )
        return bound_call

    def bring_online(self, agent_id: str) -> bool:
        return self.transition(agent_id, AgentState.AVAILABLE, expected_state=AgentState.OFFLINE)

    # -- reconciliation / crash recovery --------------------------------------

    def reclaim_stale_reservations(self) -> int:
        """A worker that reserved (or was dialing with) an agent and then
        crashed never renews the lease. Once the lease expires, this
        background reaper can safely reclaim the agent. This is what
        prevents a crashed worker from permanently leaking an agent."""
        now = time.time()
        cur = self.db.execute_with_retry(
            "UPDATE agents SET state=?, worker_id=NULL, call_id=NULL, "
            "lease_expires_at=NULL, version=version+1, updated_at=? "
            "WHERE state IN (?, ?) AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
            (AgentState.AVAILABLE, now, AgentState.RESERVED, AgentState.DIALING, now),
        )
        return cur.rowcount

    def release_finished_wrap_ups(self) -> int:
        now = time.time()
        cur = self.db.execute_with_retry(
            "UPDATE agents SET state=?, call_id=NULL, wrap_until=NULL, "
            "version=version+1, updated_at=? "
            "WHERE state=? AND wrap_until IS NOT NULL AND wrap_until < ?",
            (AgentState.AVAILABLE, now, AgentState.WRAP_UP, now),
        )
        return cur.rowcount

    # -- reads -----------------------------------------------------------------

    def counts(self) -> dict:
        conn = self.db.conn()
        rows = conn.execute("SELECT state, COUNT(*) c FROM agents GROUP BY state").fetchall()
        out = {s: 0 for s in (AgentState.OFFLINE, AgentState.AVAILABLE, AgentState.RESERVED,
                               AgentState.DIALING, AgentState.CONNECTED, AgentState.WRAP_UP,
                               AgentState.PAUSED)}
        for r in rows:
            out[r["state"]] = r["c"]
        return out

    def get(self, agent_id: str):
        return self.db.conn().execute("SELECT * FROM agents WHERE agent_id=?",
                                       (agent_id,)).fetchone()

    def total(self) -> int:
        row = self.db.conn().execute("SELECT COUNT(*) c FROM agents").fetchone()
        return row["c"]
