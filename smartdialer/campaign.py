"""
Campaign: wires every layer together and runs the control loops.

    Campaign
       |  (pacing loop, every tick_interval)
       v
    Pacing Engine  ->  Safety Controller  ->  Call Allocator  ->  Provider
       ^                                          |
       |                                          v
       +------------ RollingStats <---- event worker pool <---- provider events

Three background loops:

  1. Pacing loop (1 thread): every tick, reclaim stale state, build a
     fresh Snapshot, ask the pacing engine for a number, get it checked
     by the Safety Controller, hand the approved number to the allocator.

  2. Event workers (N threads): drain the provider-event queue and apply
     each event to the call state machine, reacting with agent-side
     effects. Multiple workers here is what gives us genuine concurrent,
     possibly-out-of-order processing of events for different (and
     sometimes the same) calls -- not just a simulated illusion of it.

  3. Reaper (folded into the pacing loop tick here, since both need to
     run periodically and the prototype doesn't need a 4th thread):
     reclaims stale agent reservations, releases finished wrap-ups, and
     reconciles stale in-flight calls left behind by a "crashed" worker.
"""

from __future__ import annotations
import queue
import threading
import time
import uuid

from .models import Snapshot, DialMode
from .db import Database
from .agent_store import AgentStore
from .call_store import CallStore
from .allocator import CallAllocator
from .pacing import ProgressivePacingEngine, PredictivePacingEngine
from .safety import SafetyController
from .metrics import RollingStats, Counters, ProviderCircuitBreaker


class Campaign:
    def __init__(self, campaign_id: str, db_path: str, mode: str, provider,
                 tick_interval: float = 0.15, num_event_workers: int = 4,
                 safety: SafetyController = None, pacing_engine=None,
                 wrap_up_seconds: float = 1.0, answer_bind_grace_seconds: float = 0.4):
        self.campaign_id = campaign_id
        self.mode = mode
        self.db = Database(db_path)
        self.agents = AgentStore(self.db)
        self.calls = CallStore(self.db)
        self.stats = RollingStats()
        self.counters = Counters()
        self.circuit = ProviderCircuitBreaker()
        self.provider = provider
        self.safety = safety or SafetyController()
        self.pacing_engine = pacing_engine or (
            ProgressivePacingEngine() if mode == DialMode.PROGRESSIVE else PredictivePacingEngine())
        self.allocator = CallAllocator(campaign_id, self.agents, self.calls, provider,
                                        self.stats, self.counters,
                                        wrap_up_seconds=wrap_up_seconds,
                                        answer_bind_grace_seconds=answer_bind_grace_seconds)
        self.tick_interval = tick_interval
        self.num_event_workers = num_event_workers
        self.event_queue: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._threads = []
        self._consecutive_healthy_ticks = 0
        self.pacing_log = []
        self._pacing_log_lock = threading.Lock()

    # -- setup ------------------------------------------------------------

    def seed(self, num_agents: int, num_borrowers: int):
        self.agents.seed_agents([f"agent-{i}" for i in range(num_agents)])
        self.calls.seed_borrowers([f"borrower-{i}" for i in range(num_borrowers)],
                                   self.campaign_id)

    # -- snapshot / control loop --------------------------------------------

    def build_snapshot(self) -> Snapshot:
        ac = self.agents.counts()
        stats = self.stats.snapshot_values()
        health = self.provider.health()
        circuit_open = self.circuit.tick(health)
        unbound = self.calls.unbound_ringing_count(self.campaign_id)
        inflight = self.calls.inflight_count(self.campaign_id)
        queued = self.calls.queued_borrower_count(self.campaign_id)

        healthy_now = (not circuit_open
                       and health >= self.safety.provider_health_floor
                       and stats["abandon_rate"] <= self.safety.abandon_circuit_threshold)
        if healthy_now:
            self._consecutive_healthy_ticks += 1
        else:
            self._consecutive_healthy_ticks = 0

        return Snapshot(
            available_agents=ac["AVAILABLE"], reserved_agents=ac["RESERVED"],
            dialing_agents=ac["DIALING"], connected_agents=ac["CONNECTED"],
            wrap_up_agents=ac["WRAP_UP"], total_agents=self.agents.total(),
            ringing_unbound_calls=unbound, inflight_calls=inflight,
            recent_answer_rate=stats["answer_rate"], recent_abandon_rate=stats["abandon_rate"],
            avg_call_duration=stats["avg_duration"], avg_setup_time=stats["avg_setup"],
            provider_health=health, provider_circuit_open=circuit_open,
            queued_borrowers=queued, consecutive_healthy_ticks=self._consecutive_healthy_ticks,
        )

    def _log_pacing(self, request, decision):
        entry = {"ts": time.time(), "requested": request.requested, "approved": decision.approved,
                  "action": decision.action, "reason": decision.reason,
                  "pacing_explanation": request.explanation, "safety_details": decision.details}
        with self._pacing_log_lock:
            self.pacing_log.append(entry)
        self.db.execute_with_retry(
            "INSERT INTO pacing_log(ts, campaign_id, mode, requested, approved, action, reason) "
            "VALUES (?,?,?,?,?,?,?)",
            (entry["ts"], self.campaign_id, request.mode, request.requested, decision.approved,
             decision.action, decision.reason),
        )
        self.counters.inc(f"safety_{decision.action.lower()}")

    def _on_event(self, ev):
        self.event_queue.put(ev)

    # -- background loops ----------------------------------------------------

    def _pacing_loop(self):
        worker_id = f"pacer-{self.campaign_id}"
        while not self._stop.is_set():
            reclaimed_agents = self.agents.reclaim_stale_reservations()
            reclaimed_calls = self.calls.reclaim_stale_calls()
            self.agents.release_finished_wrap_ups()
            if reclaimed_agents:
                self.counters.inc("stale_agents_reclaimed", reclaimed_agents)
            if reclaimed_calls:
                self.counters.inc("stale_calls_reclaimed", reclaimed_calls)

            snap = self.build_snapshot()
            request = self.pacing_engine.compute_request(snap)
            decision = self.safety.evaluate(request, snap)
            self._log_pacing(request, decision)

            if decision.approved > 0:
                if decision.effective_mode == DialMode.PROGRESSIVE:
                    self.allocator.dial_progressive_batch(decision.approved, worker_id, self._on_event)
                else:
                    self.allocator.dial_predictive_batch(decision.approved, worker_id, self._on_event)

            time.sleep(self.tick_interval)

    def _event_worker_loop(self):
        while True:
            try:
                ev = self.event_queue.get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            try:
                self.allocator.handle_provider_event(ev)
            except Exception:
                self.counters.inc("event_processing_errors")
            finally:
                self.event_queue.task_done()

    # -- lifecycle -----------------------------------------------------------

    def start(self):
        t = threading.Thread(target=self._pacing_loop, daemon=True, name=f"pacer-{self.campaign_id}")
        t.start()
        self._threads.append(t)
        for i in range(self.num_event_workers):
            t = threading.Thread(target=self._event_worker_loop, daemon=True,
                                  name=f"event-worker-{self.campaign_id}-{i}")
            t.start()
            self._threads.append(t)

    def stop(self, drain_seconds: float = 1.0):
        time.sleep(drain_seconds)  # let in-flight provider events land
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)

    # -- scenario helpers used by simulate.py / tests -------------------------

    def simulate_agents_disappear(self, n: int):
        """Force N currently-non-offline agents OFFLINE, cascade-failing
        any call they were bound to. Used for the 'sudden availability
        drop' failure scenario."""
        conn = self.db.conn()
        rows = conn.execute(
            "SELECT agent_id FROM agents WHERE state != 'OFFLINE' LIMIT ?", (n,)).fetchall()
        affected_calls = []
        for r in rows:
            bound_call = self.agents.set_offline(r["agent_id"])
            if bound_call:
                affected_calls.append(bound_call)
        for call_id in affected_calls:
            self.calls.apply_event(call_id, "FAILED", event_key=f"{call_id}:agent-vanished")
            self.counters.inc("calls_failed_agent_vanished")
        self.counters.inc("agents_forced_offline", len(rows))
        return len(rows)

    def recent_pacing_log(self, n: int = 20) -> list:
        """Thread-safe read of the last N in-memory pacing decisions, for
        live consumers (e.g. dashboard.py) that shouldn't reach into the
        lock directly."""
        with self._pacing_log_lock:
            return list(self.pacing_log[-n:])

    def report(self) -> dict:
        return {
            "agent_counts": self.agents.counts(),
            "call_counts": self.calls.counts(self.campaign_id),
            "stats": self.stats.snapshot_values(),
            "provider_health": self.provider.health(),
            "circuit_open": self.circuit.is_open,
            "counters": self.counters.snapshot(),
            "queued_borrowers": self.calls.queued_borrower_count(self.campaign_id),
        }
