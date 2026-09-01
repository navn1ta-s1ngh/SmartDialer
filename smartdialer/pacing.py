"""
Pacing engines: Progressive and Predictive.

STRUCTURAL SAFETY GUARANTEE
------------------------------
Neither pacing engine imports `providers.py`, holds a reference to a
TelecomProvider, or holds a reference to the CallAllocator. Look at the
constructors below -- there is nothing to wire one in. This isn't just a
convention; it's an object-graph fact. A pacing engine can compute "I want
to start 17 calls" but it has no method, attribute, or import path that
could physically place a call. The only way calls get placed is through
`allocator.py`, and the only number allocator.py is allowed to act on is
whatever `safety.py` approved (see campaign.py's orchestration loop).

Both engines expose the same interface:

    compute_request(snapshot: Snapshot) -> PacingDecisionRequest

`explanation` on the returned request is a plain dict of the inputs and
intermediate values used, specifically so that "why did the system decide
to request N calls?" always has a concrete answer instead of a vibe.
"""

from __future__ import annotations
import math

from .models import Snapshot, PacingDecisionRequest, DialMode


class ProgressivePacingEngine:
    """The safe, boring baseline: never ask for more new calls than there
    are AVAILABLE agents right now. One call per free agent, no
    speculation. This mode literally cannot cause an abandoned connected
    call, because every call it starts already has an agent bound before
    the phone even rings."""

    def compute_request(self, snapshot: Snapshot) -> PacingDecisionRequest:
        requested = max(0, snapshot.available_agents)
        return PacingDecisionRequest(
            requested=requested,
            mode=DialMode.PROGRESSIVE,
            explanation={
                "rule": "requested = available_agents (1 call per free agent, no speculation)",
                "available_agents": snapshot.available_agents,
            },
        )


class PredictivePacingEngine:
    """Rule-based predictive pacing.

    The question this answers: given how many calls are already ringing
    and likely to be answered soon, how many *additional* calls should we
    start right now so that, by the time borrowers pick up, we have
    roughly enough agents freeing up to take them -- without assuming
    every dial converts to an answer?

    Signals used (all read off the Snapshot, never mutated here):
      - available_agents        : free agent capacity right now
      - ringing_unbound_calls   : predictive calls already in flight,
                                   not yet resolved, with no agent bound
      - recent_answer_rate      : EWMA of answer rate over recent calls
      - provider_health         : EWMA of provider success rate

    Method (deliberately simple, deliberately explainable):

      1. expected_conversions = ringing_unbound_calls * recent_answer_rate
         -- how many of the calls already in flight we expect to need an
         agent soon.

      2. free_capacity = available_agents - expected_conversions
         -- capacity not already "spoken for" by calls in flight.
         Clamped at 0.

      3. dial_multiplier = 1 / max(recent_answer_rate, floor_rate)
         -- if only 20% of calls answer, we need ~5 dials to net one
         connect. `floor_rate` stops this exploding when the answer rate
         estimate is near zero or based on very few samples.

      4. raw = free_capacity * dial_multiplier

      5. margin = dynamic safety margin (grows when the provider is
         unhealthy, or when the abandon rate has been non-zero recently)
         raw_after_margin = raw * provider_health * (1 - margin)

      6. requested = floor(raw_after_margin)

    Every number in this chain is put in `explanation` so a pacing
    decision can always be walked through step by step.

    IMPORTANT: this is *only ever a request*. It has no way to act on it.
    The Safety Controller recomputes its own numbers independently and
    has the final word (see safety.py).
    """

    def __init__(self, floor_answer_rate: float = 0.05, base_margin: float = 0.15,
                 max_multiplier: float = 8.0):
        self.floor_answer_rate = floor_answer_rate
        self.base_margin = base_margin
        self.max_multiplier = max_multiplier

    def compute_request(self, snapshot: Snapshot) -> PacingDecisionRequest:
        p = max(snapshot.recent_answer_rate, self.floor_answer_rate)

        expected_conversions = snapshot.ringing_unbound_calls * snapshot.recent_answer_rate
        free_capacity = max(0.0, snapshot.available_agents - expected_conversions)

        dial_multiplier = min(1.0 / p, self.max_multiplier)
        raw = free_capacity * dial_multiplier

        # Dynamic safety margin: widen it when the abandon rate is
        # nonzero (we're already hurting borrowers) or the provider is
        # degraded (its numbers are less trustworthy).
        abandon_penalty = min(0.5, snapshot.recent_abandon_rate * 4.0)
        margin = min(0.9, self.base_margin + abandon_penalty)

        raw_after_margin = raw * snapshot.provider_health * (1 - margin)
        requested = max(0, math.floor(raw_after_margin))

        return PacingDecisionRequest(
            requested=requested,
            mode=DialMode.PREDICTIVE,
            explanation={
                "available_agents": snapshot.available_agents,
                "ringing_unbound_calls": snapshot.ringing_unbound_calls,
                "recent_answer_rate": round(snapshot.recent_answer_rate, 4),
                "answer_rate_used_for_division": round(p, 4),
                "expected_conversions_from_inflight": round(expected_conversions, 2),
                "free_capacity": round(free_capacity, 2),
                "dial_multiplier": round(dial_multiplier, 2),
                "raw_before_margin": round(raw, 2),
                "provider_health": snapshot.provider_health,
                "recent_abandon_rate": round(snapshot.recent_abandon_rate, 4),
                "abandon_penalty": round(abandon_penalty, 3),
                "dynamic_safety_margin": round(margin, 3),
                "formula": "requested = floor((available_agents - ringing_unbound*p) "
                           "* min(1/p, max_mult) * provider_health * (1 - margin))",
            },
        )
