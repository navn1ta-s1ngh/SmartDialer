"""
Shared enums and small data classes used across the whole system.

Keeping these in one file means the state machines (the most safety-critical
part of this system) are defined in exactly one place. Every other module
imports from here instead of re-declaring strings.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Agent state machine
# ---------------------------------------------------------------------------

class AgentState:
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"


# Explicit, validated transition table. Anything not listed here is rejected.
AGENT_TRANSITIONS = {
    AgentState.OFFLINE: {AgentState.AVAILABLE},
    # AVAILABLE -> CONNECTED is the predictive-mode "instant bind": a
    # predictive call has no agent until the borrower answers, at which
    # point we atomically hand it a free agent and go straight to
    # CONNECTED (there is no separate dialing phase for an agent that
    # wasn't holding the call). See allocator.py.
    AgentState.AVAILABLE: {AgentState.RESERVED, AgentState.PAUSED, AgentState.OFFLINE,
                            AgentState.CONNECTED},
    AgentState.RESERVED: {AgentState.DIALING, AgentState.AVAILABLE, AgentState.OFFLINE},
    AgentState.DIALING: {AgentState.CONNECTED, AgentState.AVAILABLE, AgentState.OFFLINE},
    AgentState.CONNECTED: {AgentState.WRAP_UP, AgentState.OFFLINE},
    AgentState.WRAP_UP: {AgentState.AVAILABLE, AgentState.OFFLINE},
    AgentState.PAUSED: {AgentState.AVAILABLE, AgentState.OFFLINE},
}


# ---------------------------------------------------------------------------
# Call state machine
# ---------------------------------------------------------------------------

class CallState:
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"        # borrower claimed by a worker (not agent binding)
    INITIATED = "INITIATED"      # provider has been asked to place the call
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"        # borrower picked up; may not have an agent yet
    CONNECTED = "CONNECTED"      # bridged to a live agent
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    # Not in the assignment's minimum list, added deliberately: an ANSWERED
    # call that could not be bridged to an agent within the grace window.
    # This is exactly the "abandoned connected call" compliance risk the
    # brief calls out, so it gets its own terminal state instead of being
    # silently folded into FAILED -- we want it separately countable.
    ABANDONED = "ABANDONED"


TERMINAL_CALL_STATES = {
    CallState.COMPLETED,
    CallState.FAILED,
    CallState.CANCELLED,
    CallState.ABANDONED,
}

CALL_TRANSITIONS = {
    CallState.QUEUED: {CallState.RESERVED, CallState.CANCELLED, CallState.FAILED},
    CallState.RESERVED: {CallState.INITIATED, CallState.CANCELLED, CallState.FAILED},
    CallState.INITIATED: {CallState.RINGING, CallState.FAILED, CallState.CANCELLED},
    CallState.RINGING: {CallState.ANSWERED, CallState.FAILED, CallState.CANCELLED},
    CallState.ANSWERED: {CallState.CONNECTED, CallState.ABANDONED, CallState.FAILED},
    CallState.CONNECTED: {CallState.COMPLETED, CallState.FAILED},
    CallState.COMPLETED: set(),
    CallState.FAILED: set(),
    CallState.CANCELLED: set(),
    CallState.ABANDONED: set(),
}


class DialMode:
    PROGRESSIVE = "PROGRESSIVE"
    PREDICTIVE = "PREDICTIVE"


class SafetyAction:
    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    REJECT = "REJECT"
    FALLBACK_TO_PROGRESSIVE = "FALLBACK_TO_PROGRESSIVE"


# ---------------------------------------------------------------------------
# Small value objects passed between layers
# ---------------------------------------------------------------------------

@dataclass
class ReservationResult:
    success: bool
    agent_id: str
    worker_id: str
    reason: str


@dataclass
class EventResult:
    applied: bool
    reason: str
    new_state: Optional[str] = None
    old_state: Optional[str] = None


@dataclass
class ProviderEvent:
    call_id: str
    event_type: str
    event_key: str
    provider_call_id: str = ""
    outcome: Optional[str] = None  # e.g. "timeout" for FAILED events caused by provider timeout


@dataclass
class Snapshot:
    """Read-only view of system state, computed fresh from the stores.

    Both the Pacing Engine and the Safety Controller receive a Snapshot.
    Critically, the Safety Controller does NOT trust the snapshot the
    Pacing Engine reasoned over -- it takes its own, freshly computed one
    at evaluation time. See safety.py.
    """
    available_agents: int
    reserved_agents: int
    dialing_agents: int
    connected_agents: int
    wrap_up_agents: int
    total_agents: int
    ringing_unbound_calls: int   # predictive calls in flight with no agent bound yet
    inflight_calls: int          # INITIATED + RINGING (any mode)
    recent_answer_rate: float
    recent_abandon_rate: float
    avg_call_duration: float
    avg_setup_time: float
    provider_health: float
    provider_circuit_open: bool
    queued_borrowers: int
    consecutive_healthy_ticks: int = 0


@dataclass
class PacingDecisionRequest:
    requested: int
    mode: str
    explanation: dict = field(default_factory=dict)


@dataclass
class SafetyDecision:
    approved: int
    action: str
    reason: str
    effective_mode: str          # mode the allocator should actually dial in
    details: dict = field(default_factory=dict)
