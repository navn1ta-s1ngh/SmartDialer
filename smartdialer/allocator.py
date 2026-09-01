"""
Call Allocator.

This is the ONLY module that holds a reference to a TelecomProvider and
is allowed to call it. It sits after the Safety Controller in the
pipeline (Campaign -> Pacing -> Safety -> **Allocator** -> Provider) and
only ever acts on the count the Safety Controller approved -- it never
re-derives its own "how many should I dial" number.

Two responsibilities:

1. `dial_progressive_batch` / `dial_predictive_batch`
   Turn an approved integer into concrete (agent, borrower, call) bindings
   and hand them to the provider.

2. `handle_provider_event`
   The cascading logic that reacts to a call's state changing: binding a
   free agent to a predictive call the moment it's ANSWERED (with a short
   grace period), moving agents to WRAP_UP on COMPLETED, releasing agents
   back to AVAILABLE on FAILED/CANCELLED, and feeding outcomes back into
   RollingStats so the pacing engine adapts.
"""

from __future__ import annotations
import time

from .models import AgentState, CallState, DialMode, ProviderEvent
from .agent_store import AgentStore
from .call_store import CallStore
from .providers import TelecomProvider
from .metrics import RollingStats, Counters


class CallAllocator:
    def __init__(self, campaign_id: str, agent_store: AgentStore, call_store: CallStore,
                 provider: TelecomProvider, stats: RollingStats, counters: Counters,
                 wrap_up_seconds: float = 2.0, answer_bind_grace_seconds: float = 0.4,
                 answer_bind_retry_interval: float = 0.05):
        self.campaign_id = campaign_id
        self.agents = agent_store
        self.calls = call_store
        self.provider = provider
        self.stats = stats
        self.counters = counters
        self.wrap_up_seconds = wrap_up_seconds
        self.answer_bind_grace_seconds = answer_bind_grace_seconds
        self.answer_bind_retry_interval = answer_bind_retry_interval

    def _emit(self, on_event):
        def emit(ev: ProviderEvent):
            on_event(ev)
        return emit

    # -- dialing ------------------------------------------------------------

    def dial_progressive_batch(self, n: int, worker_id: str, on_event) -> int:
        """Progressive: reserve a specific agent BEFORE the phone ever
        rings. Every call created here already has an agent bound, so it
        can never produce an abandoned-connected-call outcome."""
        dialed = 0
        for _ in range(n):
            agent_id = self.agents.try_reserve_any_available(worker_id)
            if agent_id is None:
                break
            borrower_id = self.calls.try_claim_borrower(self.campaign_id, worker_id)
            if borrower_id is None:
                self.agents.release_to_available(agent_id)
                break
            call_id = self.calls.create_call(self.campaign_id, borrower_id,
                                              DialMode.PROGRESSIVE, worker_id, agent_id=agent_id)
            self.agents.transition(agent_id, AgentState.DIALING, expected_state=AgentState.RESERVED,
                                    call_id=call_id)
            ok = self.calls.apply_event(call_id, CallState.INITIATED,
                                         event_key=f"{call_id}:initiated")
            provider_call_id = self.provider.place_call(call_id, borrower_id, self._emit(on_event))
            self.calls.mark_initiated(call_id, self.provider.name, provider_call_id) if not ok.applied else None
            self.counters.inc("calls_initiated")
            self.counters.inc("calls_initiated_progressive")
            dialed += 1
        return dialed

    def dial_predictive_batch(self, n: int, worker_id: str, on_event) -> int:
        """Predictive: no agent bound yet. We're betting that by the time
        the borrower answers, an agent will be free -- the Safety
        Controller is what keeps that bet bounded (see safety.py)."""
        dialed = 0
        for _ in range(n):
            borrower_id = self.calls.try_claim_borrower(self.campaign_id, worker_id)
            if borrower_id is None:
                break
            call_id = self.calls.create_call(self.campaign_id, borrower_id,
                                              DialMode.PREDICTIVE, worker_id, agent_id=None)
            self.calls.apply_event(call_id, CallState.INITIATED, event_key=f"{call_id}:initiated")
            provider_call_id = self.provider.place_call(call_id, borrower_id, self._emit(on_event))
            self.calls.mark_initiated(call_id, self.provider.name, provider_call_id)
            self.counters.inc("calls_initiated")
            self.counters.inc("calls_initiated_predictive")
            dialed += 1
        return dialed

    # -- reacting to provider events -----------------------------------------

    def handle_provider_event(self, event: ProviderEvent):
        """Apply the raw event to the call state machine, then run
        whatever cascading agent-side effect that transition implies.
        Called from campaign.py's event-processing worker pool."""
        result = self.calls.apply_event(event.call_id, event.event_type, event.event_key)
        self.counters.inc("provider_events_received")
        if not result.applied:
            self.counters.inc("provider_events_deduped_or_stale")
            return

        call = self.calls.get(event.call_id)
        if call is None:
            return

        if result.old_state == CallState.RINGING and result.new_state in (
                CallState.ANSWERED, CallState.FAILED):
            self.stats.record_ring_outcome(answered=(result.new_state == CallState.ANSWERED))
            if call["created_at"]:
                self.stats.record_setup(max(0.0, time.time() - call["created_at"]))

        if result.new_state == CallState.ANSWERED:
            self.counters.inc("calls_answered")
            self._handle_answered(event.call_id, call)
        elif result.new_state == CallState.COMPLETED:
            self.counters.inc("calls_completed")
            self._handle_completed(event.call_id, call)
        elif result.new_state == CallState.FAILED:
            self.counters.inc("calls_failed")
            self._handle_terminal_release(call)
        elif result.new_state == CallState.CANCELLED:
            self.counters.inc("calls_cancelled")
            self._handle_terminal_release(call)
        elif result.new_state == CallState.ABANDONED:
            self.counters.inc("calls_abandoned")
            self.stats.record_conversion_outcome(abandoned=True)

    def _handle_answered(self, call_id: str, call):
        if call["agent_id"]:
            # Progressive: agent was already reserved for this call.
            # Answering just bridges it -- guaranteed to succeed.
            self.agents.transition(call["agent_id"], AgentState.CONNECTED,
                                    expected_state=AgentState.DIALING)
            self.calls.apply_event(call_id, CallState.CONNECTED,
                                    event_key=f"{call_id}:auto-connected")
            self.stats.record_conversion_outcome(abandoned=False)
            return

        # Predictive: try to grab a free agent right now; if none is free,
        # retry for a short grace window before declaring the call
        # abandoned. This grace window is intentionally short -- a long
        # wait with a borrower on a live, unattended line is exactly the
        # compliance risk we're trying to avoid, not a workaround for it.
        deadline = time.time() + self.answer_bind_grace_seconds
        agent_id = None
        while time.time() < deadline:
            agent_id = self.agents.try_connect_any_available(call_id)
            if agent_id:
                break
            time.sleep(self.answer_bind_retry_interval)

        if agent_id is None:
            self.calls.apply_event(call_id, CallState.ABANDONED,
                                    event_key=f"{call_id}:abandoned")
            self.calls.set_outcome(call_id, "no_agent_available_at_answer")
            self.counters.inc("calls_abandoned")
            self.stats.record_conversion_outcome(abandoned=True)
            return

        self.calls.bind_agent(call_id, agent_id)
        self.calls.apply_event(call_id, CallState.CONNECTED, event_key=f"{call_id}:auto-connected")
        self.stats.record_conversion_outcome(abandoned=False)

    def _handle_completed(self, call_id: str, call):
        if call["agent_id"]:
            self.agents.transition(call["agent_id"], AgentState.WRAP_UP,
                                    expected_state=AgentState.CONNECTED,
                                    wrap_seconds=self.wrap_up_seconds)
        if call["connected_at"]:
            self.stats.record_duration(max(0.0, time.time() - call["connected_at"]))

    def _handle_terminal_release(self, call):
        if call["agent_id"]:
            self.agents.release_to_available(call["agent_id"])
