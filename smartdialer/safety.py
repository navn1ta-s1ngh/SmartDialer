"""
Safety Controller: the one non-negotiable boundary in this system.

WHAT IT DOES AND DOES NOT TRUST
----------------------------------
The Safety Controller receives a PacingDecisionRequest (a number and an
explanation from whichever pacing engine produced it) but it does NOT
trust the numbers embedded in that request's explanation. It takes its
own fresh Snapshot (built in campaign.py, straight from AgentStore /
CallStore -- the actual source of truth) at evaluation time and computes
its own idea of "safe capacity" from scratch. The only thing it takes
from the pacing engine is the single integer `requested`.

Concretely: `evaluate()` never reads `request.explanation` for anything
that affects the approved number. It only reads `request.requested` and
`request.mode`. This is what makes "the predictive engine lied about
agent availability" a non-issue -- the Safety Controller never believed
it in the first place.

WHAT IT CAN DO
------------------
  - APPROVE:               requested <= safe capacity, dial as asked.
  - REDUCE:                requested > safe capacity, dial safe capacity.
  - REJECT:                safe capacity is 0 right now.
  - FALLBACK_TO_PROGRESSIVE: conditions are unsafe for *any* speculative
    (agent-unbound) dialing, so regardless of what predictive pacing
    asked for, only progressive-style (agent pre-bound) capacity is
    approved. This reuses the exact same evaluate() path -- fallback is
    a possible *output* of the safety check, not a separate code path
    that predictive logic could route around.

HARD CAPACITY MODEL
------------------------
For PROGRESSIVE requests: capacity is simply `available_agents`. You
cannot progressive-dial more than you have free agents, full stop.

For PREDICTIVE requests: predictive calls are placed WITHOUT a bound
agent (see call_store.create_call / pacing.py docstring) and only try to
bind an agent when the borrower answers. The danger is an ANSWERED call
with nowhere to route it -- an abandoned connected call, which the brief
calls a potential compliance issue. So the Safety Controller caps how
many *unbound* predictive calls may be in flight at once:

    overdial_factor = BASE_OVERDIAL_FACTOR
                       * provider_health
                       * (0 if circuit_open else 1)
                       * (0 if recent_abandon_rate > ABANDON_CIRCUIT_THRESHOLD else 1)

    safe_unbound_capacity = max(0, floor(available_agents * overdial_factor)
                                    - ringing_unbound_calls_already_in_flight)

If `overdial_factor` collapses to 0 (unhealthy provider, open circuit, or
recent abandonment), `safe_unbound_capacity` collapses to 0 too, which
means "no more unbound speculative dials" -- i.e. FALLBACK_TO_PROGRESSIVE
mode kicks in structurally, not via a special-cased if-statement bypass.

Hysteresis: once in fallback, we require several consecutive healthy
Snapshots (tracked by campaign.py as `consecutive_healthy_ticks`) before
re-enabling predictive overdial, so the system doesn't flap approve /
fallback / approve every tick when a metric is hovering near a threshold.
"""

from __future__ import annotations
import math

from .models import Snapshot, PacingDecisionRequest, SafetyDecision, SafetyAction, DialMode


class SafetyController:
    def __init__(self, base_overdial_factor: float = 0.5,
                 abandon_circuit_threshold: float = 0.03,
                 provider_health_floor: float = 0.55,
                 hysteresis_ticks_required: int = 3,
                 hard_max_per_tick: int = 500):
        self.base_overdial_factor = base_overdial_factor
        self.abandon_circuit_threshold = abandon_circuit_threshold
        self.provider_health_floor = provider_health_floor
        self.hysteresis_ticks_required = hysteresis_ticks_required
        self.hard_max_per_tick = hard_max_per_tick

    def evaluate(self, request: PacingDecisionRequest, snapshot: Snapshot) -> SafetyDecision:
        requested = max(0, request.requested)

        if request.mode == DialMode.PROGRESSIVE:
            return self._evaluate_progressive(requested, snapshot)
        return self._evaluate_predictive(requested, snapshot)

    # -- progressive: simplest possible hard limit --------------------------

    def _evaluate_progressive(self, requested: int, snapshot: Snapshot) -> SafetyDecision:
        safe = min(snapshot.available_agents, self.hard_max_per_tick)
        approved = min(requested, safe)
        action = SafetyAction.APPROVE if approved == requested else (
            SafetyAction.REJECT if approved == 0 else SafetyAction.REDUCE)
        return SafetyDecision(
            approved=approved, action=action,
            reason=f"progressive hard cap = available_agents ({snapshot.available_agents})",
            effective_mode=DialMode.PROGRESSIVE,
            details={"requested": requested, "safe_capacity": safe,
                     "available_agents": snapshot.available_agents},
        )

    # -- predictive: independently recomputed overdial capacity -------------

    def _evaluate_predictive(self, requested: int, snapshot: Snapshot) -> SafetyDecision:
        unsafe_reasons = []

        provider_ok = 1.0
        if snapshot.provider_circuit_open:
            provider_ok = 0.0
            unsafe_reasons.append("provider circuit breaker is OPEN")
        elif snapshot.provider_health < self.provider_health_floor:
            provider_ok = 0.0
            unsafe_reasons.append(
                f"provider_health {snapshot.provider_health:.2f} below floor "
                f"{self.provider_health_floor:.2f}")

        abandon_ok = 1.0
        if snapshot.recent_abandon_rate > self.abandon_circuit_threshold:
            abandon_ok = 0.0
            unsafe_reasons.append(
                f"recent_abandon_rate {snapshot.recent_abandon_rate:.3f} above threshold "
                f"{self.abandon_circuit_threshold:.3f}")

        hysteresis_ok = 1.0
        in_recovery = (provider_ok == 1.0 and abandon_ok == 1.0
                       and snapshot.consecutive_healthy_ticks < self.hysteresis_ticks_required)
        if in_recovery:
            hysteresis_ok = 0.0
            unsafe_reasons.append(
                f"recovering from an unsafe condition: only "
                f"{snapshot.consecutive_healthy_ticks}/{self.hysteresis_ticks_required} "
                f"consecutive healthy ticks so far")

        overdial_factor = (self.base_overdial_factor * snapshot.provider_health
                            * provider_ok * abandon_ok * hysteresis_ok)

        safe_unbound = max(
            0,
            math.floor(snapshot.available_agents * overdial_factor)
            - snapshot.ringing_unbound_calls,
        )
        safe_unbound = min(safe_unbound, self.hard_max_per_tick)

        details = {
            "requested": requested,
            "available_agents": snapshot.available_agents,
            "ringing_unbound_calls": snapshot.ringing_unbound_calls,
            "provider_health": snapshot.provider_health,
            "provider_circuit_open": snapshot.provider_circuit_open,
            "recent_abandon_rate": round(snapshot.recent_abandon_rate, 4),
            "overdial_factor": round(overdial_factor, 4),
            "safe_unbound_capacity": safe_unbound,
            "unsafe_reasons": unsafe_reasons,
        }

        if overdial_factor <= 1e-9:
            # No speculative headroom at all right now -- fall back to
            # progressive-equivalent behaviour: only as many calls as we
            # have agents free for, all pre-bound.
            approved = min(snapshot.available_agents, self.hard_max_per_tick)
            return SafetyDecision(
                approved=approved,
                action=SafetyAction.FALLBACK_TO_PROGRESSIVE,
                reason="unsafe for predictive overdial (" + "; ".join(unsafe_reasons) + "); "
                       f"falling back to progressive-equivalent capacity ({approved})",
                effective_mode=DialMode.PROGRESSIVE,
                details=details,
            )

        approved = min(requested, safe_unbound)
        if approved == requested and requested > 0:
            action = SafetyAction.APPROVE
            reason = f"requested ({requested}) within safe unbound capacity ({safe_unbound})"
        elif approved == 0:
            action = SafetyAction.REJECT
            reason = f"safe unbound capacity is 0 (available={snapshot.available_agents}, " \
                      f"already in flight={snapshot.ringing_unbound_calls}, " \
                      f"overdial_factor={overdial_factor:.3f})"
        else:
            action = SafetyAction.REDUCE
            reason = f"requested {requested} exceeds safe unbound capacity {safe_unbound}; " \
                      f"reduced to {approved}"

        return SafetyDecision(
            approved=approved, action=action, reason=reason,
            effective_mode=DialMode.PREDICTIVE, details=details,
        )
