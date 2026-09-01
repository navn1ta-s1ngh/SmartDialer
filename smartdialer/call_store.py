"""
Call store: borrower claiming + the call state machine + idempotency.

THE CORE GUARANTEE THIS FILE PROVIDES
----------------------------------------
Provider events are untrustworthy: they can duplicate, arrive out of
order, or never arrive. `apply_event` is written so that no matter what
garbage sequence comes in, at most one *logical* state transition happens
per real-world event, and a call can never move backwards or "resurrect"
after reaching a terminal state.

Two independent defenses are layered:

1. Idempotency-key dedup (`processed_events` table, PRIMARY KEY on
   event_key). If the exact same event is redelivered with the same key,
   the INSERT fails and we bail out immediately. Cheap, catches true
   retries.

2. Transition-table validation (CALL_TRANSITIONS). Even if a duplicate
   arrives with a *new* key (a provider that doesn't give a stable
   idempotency key, or a genuinely out-of-order event), we only ever
   apply an event that is a legal forward transition from the call's
   *current* state. "ANSWERED" arriving while the call is already
   ANSWERED is a no-op. "RINGING" arriving after the call already
   reached COMPLETED is discarded as stale. This defense doesn't depend
   on the provider sending well-formed idempotency keys at all.

Both the dedup INSERT and the state UPDATE are single atomic statements
guarded by SQLite's writer serialization, so two workers processing
events for the same call concurrently can't race each other either.
"""

from __future__ import annotations
import time
import uuid
from typing import Optional

from .models import CallState, CALL_TRANSITIONS, TERMINAL_CALL_STATES, EventResult
from .db import Database


class CallStore:
    def __init__(self, db: Database):
        self.db = db

    # -- borrowers --------------------------------------------------------

    def seed_borrowers(self, borrower_ids, campaign_id: str):
        now = time.time()
        conn = self.db.conn()
        for bid in borrower_ids:
            conn.execute(
                "INSERT OR REPLACE INTO borrowers(borrower_id, campaign_id, state, "
                "claimed_by, version, updated_at) VALUES (?, ?, 'QUEUED', NULL, 0, ?)",
                (bid, campaign_id, now),
            )

    def try_claim_borrower(self, campaign_id: str, worker_id: str,
                            max_attempts: int = 5) -> Optional[str]:
        """Same optimistic-claim pattern as agent reservation: pick a
        candidate, try an atomic conditional UPDATE, retry on lost race.
        This is what stops two workers from calling the same borrower
        twice at the same time."""
        conn = self.db.conn()
        for _ in range(max_attempts):
            row = conn.execute(
                "SELECT borrower_id FROM borrowers WHERE campaign_id=? AND state='QUEUED' "
                "ORDER BY updated_at LIMIT 1",
                (campaign_id,),
            ).fetchone()
            if row is None:
                return None
            bid = row["borrower_id"]
            now = time.time()
            cur = self.db.execute_with_retry(
                "UPDATE borrowers SET state='CLAIMED', claimed_by=?, version=version+1, "
                "updated_at=? WHERE borrower_id=? AND state='QUEUED'",
                (worker_id, now, bid),
            )
            if cur.rowcount == 1:
                return bid
        return None

    def queued_borrower_count(self, campaign_id: str) -> int:
        row = self.db.conn().execute(
            "SELECT COUNT(*) c FROM borrowers WHERE campaign_id=? AND state='QUEUED'",
            (campaign_id,)).fetchone()
        return row["c"]

    # -- call creation ------------------------------------------------------

    def create_call(self, campaign_id: str, borrower_id: str, mode: str,
                     worker_id: str, agent_id: Optional[str] = None,
                     lease_seconds: float = 20.0) -> str:
        call_id = str(uuid.uuid4())
        now = time.time()
        # Progressive calls already carry a bound agent -> start at
        # RESERVED (agent+borrower both claimed) then the allocator
        # immediately advances to INITIATED once it hands off to the
        # provider. Predictive calls skip agent binding entirely here.
        state = CallState.RESERVED if agent_id is not None else CallState.QUEUED
        self.db.conn().execute(
            "INSERT INTO calls(call_id, campaign_id, borrower_id, agent_id, state, mode, "
            "provider_name, provider_call_id, worker_id, lease_expires_at, version, "
            "created_at, updated_at, answered_at, connected_at, ended_at, outcome) "
            "VALUES (?,?,?,?,?,?,NULL,NULL,?,?,0,?,?,NULL,NULL,NULL,NULL)",
            (call_id, campaign_id, borrower_id, agent_id, state, mode, worker_id,
             now + lease_seconds, now, now),
        )
        if state == CallState.QUEUED:
            # predictive calls: move straight to RESERVED (borrower claimed)
            self.apply_event(call_id, CallState.RESERVED, event_key=f"{call_id}:auto-reserved")
        return call_id

    def mark_initiated(self, call_id: str, provider_name: str, provider_call_id: str) -> bool:
        now = time.time()
        cur = self.db.execute_with_retry(
            "UPDATE calls SET state=?, provider_name=?, provider_call_id=?, updated_at=? "
            "WHERE call_id=? AND state=?",
            (CallState.INITIATED, provider_name, provider_call_id, now,
             call_id, CallState.RESERVED),
        )
        return cur.rowcount == 1

    # -- the critical section: idempotent event application -------------------

    def apply_event(self, call_id: str, event_type: str, event_key: Optional[str] = None) -> EventResult:
        event_key = event_key or f"{call_id}:{event_type}:{uuid.uuid4().hex[:8]}"
        conn = self.db.conn()

        # Defense 1: exact-duplicate dedup via idempotency key.
        try:
            conn.execute(
                "INSERT INTO processed_events(event_key, call_id, event_type, received_at) "
                "VALUES (?,?,?,?)",
                (event_key, call_id, event_type, time.time()),
            )
        except Exception:
            return EventResult(False, "duplicate event_key: already processed")

        row = conn.execute("SELECT state FROM calls WHERE call_id=?", (call_id,)).fetchone()
        if row is None:
            return EventResult(False, "unknown call_id")
        current = row["state"]

        # Defense 2: transition-table validation (catches duplicates
        # delivered with a fresh key, and genuinely out-of-order events).
        if event_type == current:
            return EventResult(False, f"no-op: call already in {current}", new_state=current,
                                old_state=current)
        if current in TERMINAL_CALL_STATES:
            return EventResult(False, f"stale event ignored: call already terminal ({current})",
                                new_state=current, old_state=current)
        if event_type not in CALL_TRANSITIONS.get(current, set()):
            return EventResult(False,
                                f"stale/out-of-order event ignored: {current} -> {event_type} "
                                f"is not a legal transition", new_state=current, old_state=current)

        now = time.time()
        extra_cols, extra_vals = "", []
        if event_type == CallState.ANSWERED:
            extra_cols, extra_vals = ", answered_at=?", [now]
        elif event_type == CallState.CONNECTED:
            extra_cols, extra_vals = ", connected_at=?", [now]
        elif event_type in TERMINAL_CALL_STATES:
            extra_cols, extra_vals = ", ended_at=?", [now]

        cur = self.db.execute_with_retry(
            f"UPDATE calls SET state=?, updated_at=?{extra_cols} WHERE call_id=? AND state=?",
            tuple([event_type, now] + extra_vals + [call_id, current]),
        )
        if cur.rowcount == 0:
            # Another thread transitioned this call between our SELECT and
            # our UPDATE. Safe to report as a race-but-harmless no-op.
            return EventResult(False, "race detected on call row, no-op", new_state=None,
                                old_state=current)
        return EventResult(True, "applied", new_state=event_type, old_state=current)

    def bind_agent(self, call_id: str, agent_id: str) -> bool:
        cur = self.db.execute_with_retry(
            "UPDATE calls SET agent_id=?, updated_at=? WHERE call_id=? AND agent_id IS NULL",
            (agent_id, time.time(), call_id),
        )
        return cur.rowcount == 1

    def set_outcome(self, call_id: str, outcome: str):
        self.db.execute_with_retry(
            "UPDATE calls SET outcome=? WHERE call_id=?", (outcome, call_id),
        )

    def reclaim_stale_calls(self) -> int:
        """Worker crashed mid-flight and its lease expired -> the call is
        moved to FAILED (outcome='stale_reconciled') so it stops counting
        as in-flight capacity. This is what prevents a crashed worker
        from leaking a phantom in-flight call forever."""
        now = time.time()
        conn = self.db.conn()
        rows = conn.execute(
            "SELECT call_id FROM calls WHERE state IN (?,?,?) "
            "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
            (CallState.RESERVED, CallState.INITIATED, CallState.RINGING, now),
        ).fetchall()
        n = 0
        for r in rows:
            cur = self.db.execute_with_retry(
                "UPDATE calls SET state=?, outcome=?, ended_at=?, updated_at=? "
                "WHERE call_id=? AND state NOT IN (?,?,?,?)",
                (CallState.FAILED, "stale_reconciled", now, now, r["call_id"],
                 CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED, CallState.ABANDONED),
            )
            n += cur.rowcount
        return n

    # -- reads -------------------------------------------------------------------

    def get(self, call_id: str):
        return self.db.conn().execute("SELECT * FROM calls WHERE call_id=?", (call_id,)).fetchone()

    def counts(self, campaign_id: str) -> dict:
        rows = self.db.conn().execute(
            "SELECT state, COUNT(*) c FROM calls WHERE campaign_id=? GROUP BY state",
            (campaign_id,)).fetchall()
        return {r["state"]: r["c"] for r in rows}

    def unbound_ringing_count(self, campaign_id: str) -> int:
        row = self.db.conn().execute(
            "SELECT COUNT(*) c FROM calls WHERE campaign_id=? AND agent_id IS NULL "
            "AND state IN (?,?,?)",
            (campaign_id, CallState.INITIATED, CallState.RINGING, CallState.ANSWERED),
        ).fetchone()
        return row["c"]

    def inflight_count(self, campaign_id: str) -> int:
        row = self.db.conn().execute(
            "SELECT COUNT(*) c FROM calls WHERE campaign_id=? AND state IN (?,?)",
            (campaign_id, CallState.INITIATED, CallState.RINGING),
        ).fetchone()
        return row["c"]

    def recent_terminal_calls(self, campaign_id: str, limit: int = 50):
        return self.db.conn().execute(
            "SELECT * FROM calls WHERE campaign_id=? AND state IN (?,?,?,?) "
            "ORDER BY updated_at DESC LIMIT ?",
            (campaign_id, CallState.COMPLETED, CallState.FAILED, CallState.ABANDONED,
             CallState.CANCELLED, limit),
        ).fetchall()
