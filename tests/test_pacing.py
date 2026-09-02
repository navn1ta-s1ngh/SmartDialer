import inspect

from engine.models import Snapshot, DialMode
from engine.pacing import ProgressivePacingEngine, PredictivePacingEngine
import engine.providers as providers_module


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


def test_progressive_requests_exactly_available_agents():
    engine = ProgressivePacingEngine()
    req = engine.compute_request(make_snapshot(available_agents=7))
    assert req.requested == 7
    assert req.mode == DialMode.PROGRESSIVE


def test_predictive_request_is_zero_with_no_available_agents():
    engine = PredictivePacingEngine()
    req = engine.compute_request(make_snapshot(available_agents=0))
    assert req.requested == 0


def test_predictive_request_increases_with_more_available_agents():
    engine = PredictivePacingEngine()
    low = engine.compute_request(make_snapshot(available_agents=5))
    high = engine.compute_request(make_snapshot(available_agents=50))
    assert high.requested > low.requested


def test_predictive_request_decreases_as_answer_rate_drops():
    """Lower answer rate should mean the safety margin / conversion
    logic still allows dialing (more dials needed per connect) up to a
    point, but a badly degraded provider_health or abandon rate should
    suppress it. Here we isolate the effect of provider_health."""
    engine = PredictivePacingEngine()
    healthy = engine.compute_request(make_snapshot(provider_health=1.0))
    degraded = engine.compute_request(make_snapshot(provider_health=0.3))
    assert degraded.requested < healthy.requested


def test_predictive_request_shrinks_when_abandon_rate_rises():
    engine = PredictivePacingEngine()
    clean = engine.compute_request(make_snapshot(recent_abandon_rate=0.0))
    abandoning = engine.compute_request(make_snapshot(recent_abandon_rate=0.2))
    assert abandoning.requested < clean.requested


def test_predictive_explanation_is_populated_and_explainable():
    engine = PredictivePacingEngine()
    req = engine.compute_request(make_snapshot(available_agents=17))
    for key in ("available_agents", "recent_answer_rate", "dial_multiplier",
                "dynamic_safety_margin", "formula"):
        assert key in req.explanation


def test_in_flight_unbound_calls_reduce_the_request():
    engine = PredictivePacingEngine()
    none_inflight = engine.compute_request(make_snapshot(available_agents=20, ringing_unbound_calls=0))
    lots_inflight = engine.compute_request(make_snapshot(available_agents=20, ringing_unbound_calls=15))
    assert lots_inflight.requested < none_inflight.requested


def test_pacing_engines_have_no_access_to_a_provider_or_allocator():
    """Structural safety check: a pacing engine object must not carry any
    attribute that is (or could call into) a TelecomProvider, and the
    module must not even import providers.py. This is what makes 'the
    predictive engine bypasses the Safety Controller and calls the
    provider directly' structurally impossible, not just discouraged by
    convention."""
    import engine.pacing as pacing_module
    assert "providers" not in pacing_module.__dict__, \
        "pacing.py must not import providers.py at all"

    for engine in (ProgressivePacingEngine(), PredictivePacingEngine()):
        for attr_name in vars(engine):
            attr = getattr(engine, attr_name)
            assert not isinstance(attr, providers_module.TelecomProvider), (
                f"{engine.__class__.__name__}.{attr_name} holds a TelecomProvider reference"
            )
        # and no method on the class should reference a symbol named
        # anything provider/allocator related
        src = inspect.getsource(engine.__class__)
        assert "provider.place_call" not in src
        assert "self.provider" not in src
        assert "self.allocator" not in src
