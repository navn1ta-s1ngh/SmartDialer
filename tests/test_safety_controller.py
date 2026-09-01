import pytest

from smartdialer.models import Snapshot, PacingDecisionRequest, DialMode, SafetyAction
from smartdialer.safety import SafetyController


def make_snapshot(**overrides):
    base = dict(
        available_agents=10, reserved_agents=0, dialing_agents=0, connected_agents=0,
        wrap_up_agents=0, total_agents=10, ringing_unbound_calls=0, inflight_calls=0,
        recent_answer_rate=0.3, recent_abandon_rate=0.0, avg_call_duration=90.0,
        avg_setup_time=1.0, provider_health=1.0, provider_circuit_open=False,
        queued_borrowers=100, consecutive_healthy_ticks=10,
    )
    base.update(overrides)
    return Snapshot(**base)


def test_progressive_never_exceeds_available_agents():
    sc = SafetyController()
    snap = make_snapshot(available_agents=5)
    req = PacingDecisionRequest(requested=50, mode=DialMode.PROGRESSIVE)
    decision = sc.evaluate(req, snap)
    assert decision.approved == 5
    assert decision.action == SafetyAction.REDUCE


def test_progressive_approves_when_within_capacity():
    sc = SafetyController()
    snap = make_snapshot(available_agents=5)
    req = PacingDecisionRequest(requested=3, mode=DialMode.PROGRESSIVE)
    decision = sc.evaluate(req, snap)
    assert decision.approved == 3
    assert decision.action == SafetyAction.APPROVE


def test_predictive_request_gets_reduced_not_trusted_blindly():
    """This is the exact example from the assignment brief: pacing wants
    17, safety controller independently determines a smaller safe number."""
    sc = SafetyController(base_overdial_factor=0.5)
    snap = make_snapshot(available_agents=20, ringing_unbound_calls=0, provider_health=1.0)
    req = PacingDecisionRequest(requested=17, mode=DialMode.PREDICTIVE)
    decision = sc.evaluate(req, snap)
    # safe_unbound = floor(20 * 0.5) - 0 = 10
    assert decision.approved == 10
    assert decision.action == SafetyAction.REDUCE
    assert decision.approved < req.requested


def test_predictive_approves_when_request_within_safe_capacity():
    sc = SafetyController(base_overdial_factor=0.5)
    snap = make_snapshot(available_agents=20)
    req = PacingDecisionRequest(requested=5, mode=DialMode.PREDICTIVE)
    decision = sc.evaluate(req, snap)
    assert decision.approved == 5
    assert decision.action == SafetyAction.APPROVE


def test_open_circuit_forces_fallback_to_progressive():
    sc = SafetyController()
    snap = make_snapshot(available_agents=8, provider_circuit_open=True)
    req = PacingDecisionRequest(requested=20, mode=DialMode.PREDICTIVE)
    decision = sc.evaluate(req, snap)
    assert decision.action == SafetyAction.FALLBACK_TO_PROGRESSIVE
    assert decision.effective_mode == DialMode.PROGRESSIVE
    assert decision.approved == 8  # capped at available agents, like progressive


def test_low_provider_health_forces_fallback():
    sc = SafetyController(provider_health_floor=0.6)
    snap = make_snapshot(available_agents=8, provider_health=0.4)
    req = PacingDecisionRequest(requested=20, mode=DialMode.PREDICTIVE)
    decision = sc.evaluate(req, snap)
    assert decision.action == SafetyAction.FALLBACK_TO_PROGRESSIVE


def test_high_abandon_rate_forces_fallback():
    sc = SafetyController(abandon_circuit_threshold=0.02)
    snap = make_snapshot(available_agents=8, recent_abandon_rate=0.1)
    req = PacingDecisionRequest(requested=20, mode=DialMode.PREDICTIVE)
    decision = sc.evaluate(req, snap)
    assert decision.action == SafetyAction.FALLBACK_TO_PROGRESSIVE


def test_hysteresis_blocks_immediate_recovery():
    """Even once health/abandon rate look fine again, we require several
    consecutive healthy ticks before re-enabling predictive overdial --
    otherwise the system could flap approve/fallback every tick."""
    sc = SafetyController(hysteresis_ticks_required=3)
    snap = make_snapshot(available_agents=10, consecutive_healthy_ticks=1)
    req = PacingDecisionRequest(requested=10, mode=DialMode.PREDICTIVE)
    decision = sc.evaluate(req, snap)
    assert decision.action == SafetyAction.FALLBACK_TO_PROGRESSIVE

    snap_recovered = make_snapshot(available_agents=10, consecutive_healthy_ticks=3)
    decision2 = sc.evaluate(req, snap_recovered)
    assert decision2.action in (SafetyAction.APPROVE, SafetyAction.REDUCE)


def test_zero_available_agents_rejects_predictive_request():
    """No agents at all -> zero safe capacity, but this is an ordinary
    REJECT (there's simply nothing to dial), not a fallback -- fallback
    is reserved for 'predictive overdial is unsafe right now' (bad
    health/abandon rate), a different condition from 'there is currently
    zero capacity of any kind'."""
    sc = SafetyController()
    snap = make_snapshot(available_agents=0)
    req = PacingDecisionRequest(requested=5, mode=DialMode.PREDICTIVE)
    decision = sc.evaluate(req, snap)
    assert decision.approved == 0
    assert decision.action == SafetyAction.REJECT


def test_already_in_flight_unbound_calls_reduce_new_headroom():
    sc = SafetyController(base_overdial_factor=0.5)
    snap = make_snapshot(available_agents=20, ringing_unbound_calls=8)
    req = PacingDecisionRequest(requested=10, mode=DialMode.PREDICTIVE)
    decision = sc.evaluate(req, snap)
    # safe_unbound = floor(20*0.5) - 8 = 2
    assert decision.approved == 2
    assert decision.action == SafetyAction.REDUCE


def test_safety_controller_ignores_pacing_engines_own_explanation():
    """Even if the pacing engine's explanation dict claims wildly
    different numbers than the real snapshot, the Safety Controller must
    only look at `requested` and the Snapshot it was given -- never at
    request.explanation -- for its capacity math."""
    sc = SafetyController(base_overdial_factor=0.5)
    snap = make_snapshot(available_agents=4)
    lying_request = PacingDecisionRequest(
        requested=3, mode=DialMode.PREDICTIVE,
        explanation={"available_agents": 999999, "free_capacity": 999999},
    )
    decision = sc.evaluate(lying_request, snap)
    # safe_unbound = floor(4*0.5) = 2, independent of the lie in explanation
    assert decision.approved == 2
