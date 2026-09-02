"""
Telecom provider abstraction.

`TelecomProvider` is the only interface the rest of the system needs to
know about. `place_call()` is fire-and-forget: it returns a provider-side
call id immediately, and delivers RINGING / ANSWERED / FAILED / COMPLETED
events asynchronously through the `emit` callback. This mirrors how real
telecom providers work (webhooks / event streams) and means the dialer
never blocks a thread waiting on a phone call to resolve.

Two mock providers are implemented with genuinely different behaviour:

  ProviderA -- fast, reliable, low failure rate.
  ProviderB -- slower, occasional timeouts, duplicate events, and
               out-of-order event delivery.

Both run their per-call event timeline on a shared, bounded thread pool
(not one raw OS thread per call) so the system stays usable even when
simulating thousands of concurrent calls -- see load_test.py.
"""

from __future__ import annotations
import random
import threading
import time
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .models import CallState, ProviderEvent


class TelecomProvider(ABC):
    name: str

    @abstractmethod
    def place_call(self, call_id: str, borrower_number: str,
                    emit: Callable[[ProviderEvent], None]) -> str:
        """Ask the provider to start dialing. Returns a provider-side call
        id immediately. Events are delivered later via `emit`, on some
        other thread."""

    @abstractmethod
    def health(self) -> float:
        """A 0..1 rolling health score for this provider (EWMA of recent
        successes vs. failures/timeouts)."""

    @abstractmethod
    def set_outage(self, active: bool):
        """Toggle a simulated outage: elevated timeouts/failures."""


# A single shared pool across all provider instances keeps resource usage
# bounded no matter how many concurrent calls we simulate (thousands of
# raw OS threads would not).
_EXECUTOR = ThreadPoolExecutor(max_workers=256, thread_name_prefix="provider-sim")


class _MockProviderBase(TelecomProvider):
    def __init__(self, name: str, *, time_scale: float, setup_time_range,
                 ring_time_range, base_answer_rate: float, base_failure_rate: float,
                 duplicate_rate: float, reorder_rate: float, avg_talk_time: float,
                 outage_failure_rate: float = 0.9, seed: int = None):
        self.name = name
        self.time_scale = time_scale
        self.setup_time_range = setup_time_range
        self.ring_time_range = ring_time_range
        self.base_answer_rate = base_answer_rate
        self.base_failure_rate = base_failure_rate
        self.duplicate_rate = duplicate_rate
        self.reorder_rate = reorder_rate
        self.avg_talk_time = avg_talk_time
        self.outage_failure_rate = outage_failure_rate
        self._outage = False
        self._health = 1.0
        self._health_lock = threading.Lock()
        self._rand = random.Random(seed)

    # -- health / circuit breaker input --------------------------------------

    def _update_health(self, success: bool):
        decay = 0.85
        with self._health_lock:
            self._health = self._health * decay + (1.0 if success else 0.0) * (1 - decay)

    def health(self) -> float:
        with self._health_lock:
            return round(self._health, 4)

    def set_outage(self, active: bool):
        self._outage = active

    # -- call simulation --------------------------------------------------------

    def place_call(self, call_id: str, borrower_number: str,
                    emit: Callable[[ProviderEvent], None]) -> str:
        provider_call_id = f"{self.name}-{uuid.uuid4().hex[:10]}"
        _EXECUTOR.submit(self._run_timeline, call_id, provider_call_id, emit)
        return provider_call_id

    def _run_timeline(self, call_id, provider_call_id, emit):
        # IMPORTANT DISTINCTION: a borrower not picking up is a normal
        # telecom outcome and says nothing about the provider's health --
        # the provider did its job (rang the number, reported no answer).
        # A setup error / timeout is a genuine PROVIDER-side failure. We
        # tag FAILED events with an `outcome` so health scoring can tell
        # these apart; conflating them would make provider_health drop
        # during any low-answer-rate campaign even when the provider is
        # perfectly fine, which would wrongly trigger provider-based
        # fallback instead of the (separate) answer-rate-based response.
        r = self._rand
        provider_error_rate = self.outage_failure_rate if self._outage else self.base_failure_rate
        answer_rate = self.base_answer_rate * (0.15 if self._outage else 1.0)

        setup_delay = r.uniform(*self.setup_time_range)
        events = [(setup_delay, CallState.RINGING, None)]

        if r.random() < provider_error_rate:
            outcome = "timeout" if self._outage else "provider_error"
            events.append((r.uniform(*self.ring_time_range), CallState.FAILED, outcome))
        elif r.random() < answer_rate:
            events.append((r.uniform(*self.ring_time_range), CallState.ANSWERED, None))
            talk = max(5.0, r.gauss(self.avg_talk_time, self.avg_talk_time * 0.25))
            events.append((talk, CallState.COMPLETED, None))
        else:
            events.append((r.uniform(*self.ring_time_range), CallState.FAILED, "no_answer"))

        # ProviderB-style chaos: occasionally swap two adjacent events so
        # they are delivered out of order.
        if self.reorder_rate and r.random() < self.reorder_rate and len(events) >= 2:
            i = r.randrange(len(events) - 1)
            events[i], events[i + 1] = events[i + 1], events[i]

        for delay, ev, outcome in events:
            time.sleep(max(0.0, delay) * self.time_scale)
            self._deliver(call_id, provider_call_id, ev, outcome, emit)

    def _deliver(self, call_id, provider_call_id, event_type, outcome, emit):
        key = uuid.uuid4().hex
        emit(ProviderEvent(call_id, event_type, key, provider_call_id, outcome=outcome))
        # A no_answer FAILED is a healthy provider outcome; only a real
        # provider-side error/timeout counts against the health score.
        is_provider_failure = outcome in ("timeout", "provider_error")
        self._update_health(success=not is_provider_failure)

        if self.duplicate_rate and self._rand.random() < self.duplicate_rate:
            # Two flavours of duplicate, both of which the call store must
            # survive: (a) exact resend with the SAME idempotency key, and
            # (b) a "different message, same fact" resend with a NEW key
            # (e.g. the provider retried and generated a fresh event id).
            time.sleep(0.01 * self.time_scale)
            same_key = self._rand.random() < 0.5
            emit(ProviderEvent(call_id, event_type, key if same_key else uuid.uuid4().hex,
                                provider_call_id))


def make_provider_a(time_scale=0.05, seed=None) -> TelecomProvider:
    """Fast, reliable, low failure rate."""
    return _MockProviderBase(
        "ProviderA", time_scale=time_scale,
        setup_time_range=(0.3, 0.8), ring_time_range=(1.0, 3.0),
        base_answer_rate=0.45, base_failure_rate=0.05,
        duplicate_rate=0.02, reorder_rate=0.0, avg_talk_time=90,
        seed=seed,
    )


def make_custom_provider(name="CustomProvider", time_scale=0.05, seed=None, answer_rate=0.3,
                          avg_talk_time=90, base_failure_rate=0.05, duplicate_rate=0.02,
                          reorder_rate=0.0, setup_time_range=(0.3, 0.8),
                          ring_time_range=(1.0, 3.0)) -> TelecomProvider:
    """Used by the simulator to build providers matching the brief's
    scenario table (specific answer rate / talk time combinations)."""
    return _MockProviderBase(
        name, time_scale=time_scale, setup_time_range=setup_time_range,
        ring_time_range=ring_time_range, base_answer_rate=answer_rate,
        base_failure_rate=base_failure_rate, duplicate_rate=duplicate_rate,
        reorder_rate=reorder_rate, avg_talk_time=avg_talk_time, seed=seed,
    )


def make_provider_b(time_scale=0.05, seed=None) -> TelecomProvider:
    """Slower, occasional timeouts, duplicate + out-of-order events."""
    return _MockProviderBase(
        "ProviderB", time_scale=time_scale,
        setup_time_range=(0.8, 2.5), ring_time_range=(1.5, 5.0),
        base_answer_rate=0.40, base_failure_rate=0.15,
        duplicate_rate=0.12, reorder_rate=0.15, avg_talk_time=90,
        seed=seed,
    )
