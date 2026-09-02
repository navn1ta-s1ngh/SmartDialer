"""
Thin SQLite wrapper.

Why SQLite for a prototype about "distributed workers"?
----------------------------------------------------------
Because the interesting problem in this assignment is not "which database
do I use" -- it's "how do I guarantee only one worker wins a race for the
same agent." That guarantee comes from doing the check-and-set as a single
atomic UPDATE statement, e.g.:

    UPDATE agents SET state='RESERVED' WHERE agent_id=? AND state='AVAILABLE'

This is exactly the pattern you'd use against Postgres with row locking
or an optimistic version column. SQLite makes it easy to demonstrate
correctly because every writer transaction is serialized by SQLite
itself -- so the same code, pointed at Postgres with the same
WHERE-clause-guarded UPDATE, keeps its correctness guarantee. Swapping
the engine is a connection-string change, not a redesign. See
ARCHITECTURE.md for the scaling discussion and where this stops being
enough.
"""

from __future__ import annotations
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    worker_id TEXT,
    call_id TEXT,
    lease_expires_at REAL,
    wrap_until REAL,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS borrowers (
    borrower_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    state TEXT NOT NULL,            -- QUEUED / CLAIMED / DONE
    claimed_by TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    call_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    borrower_id TEXT NOT NULL,
    agent_id TEXT,
    state TEXT NOT NULL,
    mode TEXT NOT NULL,
    provider_name TEXT,
    provider_call_id TEXT,
    worker_id TEXT,
    lease_expires_at REAL,
    version INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    answered_at REAL,
    connected_at REAL,
    ended_at REAL,
    outcome TEXT
);

CREATE TABLE IF NOT EXISTS processed_events (
    event_key TEXT PRIMARY KEY,
    call_id TEXT,
    event_type TEXT,
    received_at REAL
);

CREATE TABLE IF NOT EXISTS pacing_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    campaign_id TEXT,
    mode TEXT,
    requested INTEGER,
    approved INTEGER,
    action TEXT,
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_agents_state ON agents(state);
CREATE INDEX IF NOT EXISTS idx_calls_state ON calls(state);
CREATE INDEX IF NOT EXISTS idx_calls_agent ON calls(agent_id);
CREATE INDEX IF NOT EXISTS idx_borrowers_state ON borrowers(state, campaign_id);
-- Composite indexes matching the exact (filter, order-by) shape of the
-- hot candidate-selection queries in agent_store/call_store. Without
-- these, SQLite filters using idx_agents_state / idx_borrowers_state and
-- then builds a TEMP B-TREE to sort every matching row by updated_at on
-- every single reservation attempt -- see load_test.py, this was found
-- by actually profiling the load test, not assumed up front.
CREATE INDEX IF NOT EXISTS idx_agents_state_updated ON agents(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_borrowers_campaign_state_updated
    ON borrowers(campaign_id, state, updated_at);
"""


class Database:
    """Owns one SQLite file and hands out one connection per thread.

    A connection-per-thread avoids sqlite3's "objects created in a thread
    can only be used in that same thread" restriction, while still giving
    us real cross-thread write contention -- which is what we want, since
    each "worker" in the simulation runs on its own thread.
    """

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._init_done = False
        self._ensure_schema()

    def _ensure_schema(self):
        with self._init_lock:
            if self._init_done:
                return
            conn = sqlite3.connect(self.path, timeout=30)
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.commit()
            conn.close()
            self._init_done = True

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=30000;")
            c.execute("PRAGMA journal_mode=WAL;")
            self._local.conn = c
        return c

    def execute_with_retry(self, sql, params=(), retries=8):
        """A single atomic statement, retried on transient 'database is
        locked' errors. Under heavy concurrent writes SQLite may still
        raise this even with busy_timeout set, so we back off briefly.
        This is the only retry logic in the whole codebase -- everything
        else is a single atomic UPDATE ... WHERE, so nothing else *needs*
        retrying for correctness (only for liveness under contention).
        """
        conn = self.conn()
        last_err = None
        for attempt in range(retries):
            try:
                return conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                last_err = e
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    time.sleep(0.005 * (attempt + 1))
                    continue
                raise
        raise last_err

    def close(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
