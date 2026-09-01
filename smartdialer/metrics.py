"""
Rolling statistics feeding the pacing engine and Safety Controller, plus
a small circuit breaker for provider health, plus a plain counters
object used for the simulation/load-test reports.

Everything here is EWMA-based ("adaptive answer-rate estimation using
recent observations rather than a fixed historical rate" from the brief)
specifically so a sudden real-world shift -- answer rate 70% -> 10% -- is
reflected within a handful of calls instead of being diluted by months of
history.
"""

from __future__ import annotations
import threading
from collections import defaultdict


class RollingStats:
    def __init__(self, decay: float = 0.85, prior_answer_rate: float = 0.3,
                 prior_duration: float = 90.0, prior_setup: float = 1.0):
        self.decay = decay
        self._lock = threading.Lock()
        self.answer_rate = prior_answer_rate
        self.abandon_rate = 0.0
        self.avg_duration = prior_duration
        self.avg_setup = prior_setup
        self._answer_samples = 0

    def record_ring_outcome(self, answered: bool):
        """Called once a RINGING call resolves to ANSWERED or FAILED."""
        with self._lock:
            self.answer_rate = self.decay * self.answer_rate + (1 - self.decay) * (1.0 if answered else 0.0)
            self._answer_samples += 1

    def record_conversion_outcome(self, abandoned: bool):
        """Called once an ANSWERED call resolves to CONNECTED or ABANDONED.
        This is the compliance-relevant metric: of the borrowers who
        picked up, what fraction did we fail to route to an agent?"""
        with self._lock:
            self.abandon_rate = self.decay * self.abandon_rate + (1 - self.decay) * (1.0 if abandoned else 0.0)

    def record_duration(self, seconds: float):
        with self._lock:
            self.avg_duration = self.decay * self.avg_duration + (1 - self.decay) * seconds

    def record_setup(self, seconds: float):
        with self._lock:
            self.avg_setup = self.decay * self.avg_setup + (1 - self.decay) * max(0.01, seconds)

    def snapshot_values(self):
        with self._lock:
            return dict(answer_rate=self.answer_rate, abandon_rate=self.abandon_rate,
                        avg_duration=self.avg_duration, avg_setup=self.avg_setup,
                        answer_samples=self._answer_samples)


class ProviderCircuitBreaker:
    """Classic circuit breaker with hysteresis, driven off the provider's
    own EWMA health score. Deliberately separate from the Safety
    Controller's continuous `provider_health` scaling: this gives a
    binary, sticky "definitely unhealthy, stop trusting this provider for
    speculative dials" signal that doesn't flicker on/off every tick."""

    def __init__(self, open_below: float = 0.35, close_above: float = 0.65,
                 consecutive_to_close: int = 3):
        self.open_below = open_below
        self.close_above = close_above
        self.consecutive_to_close = consecutive_to_close
        self._open = False
        self._healthy_streak = 0
        self._lock = threading.Lock()

    def tick(self, health: float) -> bool:
        with self._lock:
            if not self._open:
                if health < self.open_below:
                    self._open = True
                    self._healthy_streak = 0
            else:
                if health > self.close_above:
                    self._healthy_streak += 1
                    if self._healthy_streak >= self.consecutive_to_close:
                        self._open = False
                        self._healthy_streak = 0
                else:
                    self._healthy_streak = 0
            return self._open

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open


class Counters:
    """Plain thread-safe counters for the simulation / load-test reports."""

    def __init__(self):
        self._lock = threading.Lock()
        self._c = defaultdict(int)

    def inc(self, key: str, n: int = 1):
        with self._lock:
            self._c[key] += n

    def get(self, key: str) -> int:
        with self._lock:
            return self._c[key]

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._c)
