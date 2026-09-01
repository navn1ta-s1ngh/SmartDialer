#!/usr/bin/env python3
"""
Simulation runner.

Runs the four scenarios from the assignment brief:

    Scenario   Answer Rate   Avg Talk Time
    A          20%           120 sec
    B          50%           90 sec
    C          70%           180 sec
    D          changing conditions (agent drop, provider outage, answer
               rate crash, all mid-run)

Real wall-clock seconds are compressed with `time_scale` so a 120-second
average call takes a fraction of a real second -- the ratios between
setup/ring/talk time are preserved, only the absolute scale shrinks, so
the pacing math (which reasons in those same simulated seconds via
RollingStats) behaves the same way it would at real telecom timescales.

Determinism note: each provider's random outcomes (answer/fail/duplicate/
reorder) are seeded, so the *distribution* of call outcomes is
reproducible. Exact wall-clock interleaving of threads is not perfectly
deterministic (real OS scheduling), so tick-by-tick pacing numbers can
vary by a small amount between runs -- this is called out explicitly
rather than claiming false precision.
"""
from __future__ import annotations
import argparse
import json
import time
import os

from smartdialer.campaign import Campaign
from smartdialer.providers import make_custom_provider, make_provider_b
from smartdialer.models import DialMode
from smartdialer.safety import SafetyController


def fresh_db_path(path: str) -> str:
    """Every scenario run must start from a clean database file -- re-
    running the script (or running it twice in a row) must not silently
    accumulate calls/agents from a previous run into this run's metrics.
    That would break the 'deterministic/reproducible with a fixed seed'
    property the brief asks for."""
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)
    return path


def hr(title=""):
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


def print_report(c: Campaign, elapsed: float):
    r = c.report()
    counters = r["counters"]
    total_terminal = sum(r["call_counts"].get(s, 0) for s in
                          ("COMPLETED", "FAILED", "CANCELLED", "ABANDONED"))
    print(f"  wall time simulated       : {elapsed:.1f}s")
    print(f"  calls initiated           : {counters.get('calls_initiated', 0)} "
          f"(progressive={counters.get('calls_initiated_progressive', 0)}, "
          f"predictive={counters.get('calls_initiated_predictive', 0)})")
    print(f"  calls answered            : {counters.get('calls_answered', 0)}")
    print(f"  calls connected/completed : {r['call_counts'].get('COMPLETED', 0)} completed, "
          f"{r['call_counts'].get('CONNECTED', 0)} still connected")
    print(f"  calls failed              : {counters.get('calls_failed', 0)}")
    print(f"  calls abandoned           : {counters.get('calls_abandoned', 0)}  "
          f"<-- the compliance-risk metric")
    print(f"  agent utilization snapshot: {r['agent_counts']}")
    print(f"  recent answer rate (EWMA) : {r['stats']['answer_rate']:.3f}")
    print(f"  recent abandon rate (EWMA): {r['stats']['abandon_rate']:.3f}")
    print(f"  avg call duration (EWMA)  : {r['stats']['avg_duration']:.1f}s (sim-scaled)")
    print(f"  provider health           : {r['provider_health']:.3f}  "
          f"(circuit_open={r['circuit_open']})")
    print(f"  safety: approve/reduce/reject/fallback = "
          f"{counters.get('safety_approve', 0)}/{counters.get('safety_reduce', 0)}/"
          f"{counters.get('safety_reject', 0)}/{counters.get('safety_fallback_to_progressive', 0)}")
    print(f"  provider events received  : {counters.get('provider_events_received', 0)} "
          f"(deduped/stale ignored: {counters.get('provider_events_deduped_or_stale', 0)})")
    print(f"  stale agents/calls reclaimed: {counters.get('stale_agents_reclaimed', 0)}/"
          f"{counters.get('stale_calls_reclaimed', 0)}")

    if c.pacing_log:
        sample = c.pacing_log[len(c.pacing_log) // 2]
        print(f"  sample pacing decision (mid-run):")
        print(f"    requested={sample['requested']} approved={sample['approved']} "
              f"action={sample['action']}")
        print(f"    reason: {sample['reason']}")


def run_scenario_abc(letter, answer_rate, avg_talk_time, seconds=5.0, time_scale=0.01,
                      seed=1, num_agents=30, num_borrowers=1000, db_path=None):
    hr(f"SCENARIO {letter}: answer_rate={answer_rate:.0%}, avg_talk_time={avg_talk_time}s "
       f"(predictive mode)")
    provider = make_custom_provider(f"Provider-{letter}", time_scale=time_scale, seed=seed,
                                     answer_rate=answer_rate, avg_talk_time=avg_talk_time,
                                     base_failure_rate=0.05, duplicate_rate=0.03)
    c = Campaign(f"scenario-{letter}", fresh_db_path(db_path or f"/tmp/sim_scenario_{letter}.db"),
                 DialMode.PREDICTIVE, provider, tick_interval=0.08, num_event_workers=6)
    c.seed(num_agents=num_agents, num_borrowers=num_borrowers)
    c.start()
    t0 = time.time()
    time.sleep(seconds)
    c.stop(drain_seconds=1.0)
    print_report(c, time.time() - t0)
    return c


def run_scenario_d(seconds=8.0, time_scale=0.01, seed=99, num_agents=30, num_borrowers=1200,
                    db_path=None):
    hr("SCENARIO D: changing conditions (agent drop + provider outage + answer-rate crash)")
    provider = make_custom_provider("Provider-D", time_scale=time_scale, seed=seed,
                                     answer_rate=0.55, avg_talk_time=100,
                                     base_failure_rate=0.05, duplicate_rate=0.05,
                                     reorder_rate=0.05)
    safety = SafetyController(base_overdial_factor=0.8, hysteresis_ticks_required=3)
    c = Campaign("scenario-d", fresh_db_path(db_path or "/tmp/sim_scenario_d.db"), DialMode.PREDICTIVE,
                 provider, tick_interval=0.08, num_event_workers=6, safety=safety)
    c.seed(num_agents=num_agents, num_borrowers=num_borrowers)
    c.start()
    t0 = time.time()

    phase = seconds / 4
    time.sleep(phase)
    print(f"  [t={time.time()-t0:.1f}s] -> 15 agents suddenly disappear")
    c.simulate_agents_disappear(15)

    time.sleep(phase)
    print(f"  [t={time.time()-t0:.1f}s] -> provider outage begins")
    provider.set_outage(True)

    time.sleep(phase)
    print(f"  [t={time.time()-t0:.1f}s] -> outage ends; answer rate crashes 55% -> 8%")
    provider.set_outage(False)
    provider.base_answer_rate = 0.08

    time.sleep(phase)
    c.stop(drain_seconds=1.0)
    print_report(c, time.time() - t0)

    fallback_events = [e for e in c.pacing_log if e["action"] == "FALLBACK_TO_PROGRESSIVE"]
    print(f"\n  fallback-to-progressive decisions during the run: {len(fallback_events)}")
    if fallback_events:
        print(f"  first fallback reason: {fallback_events[0]['reason']}")
    return c


def run_provider_b_chaos_demo(seconds=4.0, time_scale=0.01, seed=5):
    hr("PROVIDER B CHAOS DEMO: duplicates + out-of-order events, does state stay consistent?")
    provider = make_provider_b(time_scale=time_scale, seed=seed)
    c = Campaign("chaos-demo", fresh_db_path("/tmp/sim_chaos.db"), DialMode.PREDICTIVE, provider,
                 tick_interval=0.08, num_event_workers=6)
    c.seed(num_agents=20, num_borrowers=600)
    c.start()
    t0 = time.time()
    time.sleep(seconds)
    c.stop(drain_seconds=1.0)
    print_report(c, time.time() - t0)
    return c


def run_safety_controller_comparison(seconds=4.0, time_scale=0.01, seed=42):
    """Not asked for explicitly, but the most convincing evidence for
    'why does the Safety Controller matter': run the SAME pacing engine,
    provider, and seed twice -- once behind the real Safety Controller,
    once behind a deliberately reckless one (high overdial allowance,
    safety checks effectively disabled) -- and compare abandonment."""
    hr("BONUS: Safety Controller ON vs a deliberately reckless one (same pacing engine, same seed)")

    def one_run(label, safety):
        provider = make_custom_provider(f"Provider-{label}", time_scale=time_scale, seed=seed,
                                         answer_rate=0.65, avg_talk_time=90, base_failure_rate=0.05)
        c = Campaign(f"cmp-{label}", fresh_db_path(f"/tmp/sim_cmp_{label}.db"), DialMode.PREDICTIVE, provider,
                     tick_interval=0.08, num_event_workers=6, safety=safety)
        c.seed(num_agents=15, num_borrowers=500)  # deliberately tight agent pool
        c.start()
        time.sleep(seconds)
        c.stop(drain_seconds=1.0)
        r = c.report()
        answered = r["counters"].get("calls_answered", 0)
        abandoned = r["counters"].get("calls_abandoned", 0)
        rate = abandoned / answered if answered else 0.0
        print(f"  [{label}] answered={answered} abandoned={abandoned} "
              f"abandon-of-answered-rate={rate:.1%}")
        return r

    safe = SafetyController()  # shipped defaults
    reckless = SafetyController(base_overdial_factor=6.0, abandon_circuit_threshold=1.0,
                                 provider_health_floor=0.0, hysteresis_ticks_required=0)
    one_run("safety_on", safe)
    one_run("safety_reckless", reckless)
    print("  Same pacing engine, same call volume, same seed -- the only difference is how much "
          "\n  the Safety Controller trusts the pacing engine's request. This is the whole point "
          "of\n  putting an independent boundary between them.")


def main():
    ap = argparse.ArgumentParser(description="Run SmartDialer simulation scenarios")
    ap.add_argument("--fast", action="store_true", help="shorter run for quick iteration")
    args = ap.parse_args()

    seconds = 2.0 if args.fast else 5.0
    d_seconds = 3.0 if args.fast else 8.0

    run_scenario_abc("A", answer_rate=0.20, avg_talk_time=120, seconds=seconds)
    run_scenario_abc("B", answer_rate=0.50, avg_talk_time=90, seconds=seconds)
    run_scenario_abc("C", answer_rate=0.70, avg_talk_time=180, seconds=seconds)
    run_scenario_d(seconds=d_seconds)
    run_provider_b_chaos_demo(seconds=seconds)
    run_safety_controller_comparison(seconds=seconds)

    hr("DONE")
    print("Full pacing decision logs were also written to each scenario's SQLite file "
          "in the pacing_log table, e.g. /tmp/sim_scenario_a.db")


if __name__ == "__main__":
    main()
