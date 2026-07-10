"""CircuitBreaker — multi-instance breaker with adaptive thresholds."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from ostiari.exceptions import AgentTerminatedError, BreakerTrippedError
from ostiari.models import (
    BreakerConfig,
    BreakerState,
    MetricSummary,
    MetricType,
)

log = logging.getLogger("ostiari")


class BreakerInstance:
    __slots__ = (
        "config",
        "state",
        "counter",
        "tripped_at",
        "test_action_allowed",
        "_lock",
        "_clock",
    )

    def __init__(self, config: BreakerConfig, clock: Callable[[], float]) -> None:
        self.config = config
        self.state: str = "closed"
        self.counter: float = 0.0
        self.tripped_at: float | None = None
        self.test_action_allowed: bool = False
        self._lock = threading.Lock()
        self._clock = clock

    def trip(self) -> None:
        with self._lock:
            self.state = "open"
            self.tripped_at = self._clock()
            self.test_action_allowed = False

    def try_half_open(self) -> bool:
        with self._lock:
            if self.state != "open":
                return False
            self.state = "half_open"
            self.test_action_allowed = False
            return True

    def probe_allowed(self) -> bool:
        with self._lock:
            if self.state == "half_open" and not self.test_action_allowed:
                self.test_action_allowed = True
                return True
            return False

    def close(self) -> None:
        with self._lock:
            self.state = "closed"
            self.counter = 0.0
            self.test_action_allowed = False

    def reopen(self) -> None:
        with self._lock:
            self.state = "open"
            self.tripped_at = self._clock()
            self.test_action_allowed = False

    @property
    def breaker_id(self) -> str:
        return self.config.metric.value


class CircuitBreaker:
    def __init__(
        self,
        storage: Any = None,
        tracer: Any = None,
        configs: list[BreakerConfig] | None = None,
        clock: Callable[[], float] | None = None,
        persist_queue: deque[tuple[str, object]] | None = None,
    ) -> None:
        self._clock = clock or time.monotonic
        self._storage = storage
        self._tracer = tracer
        self._persist_queue = persist_queue
        self._breakers: dict[str, BreakerInstance] = {}
        self._adaptive_enabled: bool = False
        self._sensitivity: float = 2.0
        self._min_samples: int = 10
        self._baseline_window: int = 100
        self._on_state_change: Callable[..., object] | None = None

        if configs:
            self.configure(configs)

    def configure(self, configs: list[BreakerConfig]) -> None:
        new_breakers: dict[str, BreakerInstance] = {}
        for cfg in configs:
            breaker_id = cfg.metric.value
            if breaker_id in self._breakers:
                existing = self._breakers[breaker_id]
                existing.config = cfg
                new_breakers[breaker_id] = existing
            else:
                new_breakers[breaker_id] = BreakerInstance(cfg, self._clock)
        self._breakers = new_breakers

    def check(self) -> str | None:
        probing_id: str | None = None

        for breaker in self._breakers.values():
            if breaker.state == "closed":
                continue

            if breaker.state == "open":
                if breaker.tripped_at is not None:
                    elapsed = self._clock() - breaker.tripped_at
                    if elapsed >= breaker.config.recovery_after_seconds:
                        breaker.try_half_open()
                        if breaker.probe_allowed():
                            probing_id = breaker.breaker_id
                            continue
                self._raise_tripped(breaker)

            elif breaker.state == "half_open":
                if breaker.probe_allowed():
                    probing_id = breaker.breaker_id
                    continue
                self._raise_tripped(breaker)

        return probing_id

    def record(self, metric: MetricType, value: float) -> None:
        breaker = self._breakers.get(metric.value)
        if breaker is None:
            return

        if breaker.state in ("open", "half_open"):
            return

        threshold = self._effective_threshold(breaker)
        tripped = False
        with breaker._lock:
            breaker.counter += value
            if breaker.counter >= threshold:
                old_state = breaker.state
                breaker.state = "open"
                breaker.tripped_at = self._clock()
                breaker.test_action_allowed = False
                tripped = True

        if tripped:
            self._on_transition(breaker, old_state, "open")

    def report_outcome(self, action: str, success: bool, error: str | None = None) -> None:
        breaker = self._breakers.get(MetricType.CONSECUTIVE_FAILURES.value)
        if breaker is None:
            log.warning(
                "[Ostiari] report_outcome called but no consecutive_failures breaker configured"
            )
            return

        threshold = self._effective_threshold(breaker)
        tripped = False
        with breaker._lock:
            if success:
                breaker.counter = 0.0
            else:
                breaker.counter += 1.0
            if breaker.counter >= threshold:
                old_state = breaker.state
                breaker.state = "open"
                breaker.tripped_at = self._clock()
                breaker.test_action_allowed = False
                tripped = True

        if tripped:
            self._on_transition(breaker, old_state, "open")

    def report_probe_result(self, breaker_id: str, success: bool) -> None:
        breaker = self._breakers.get(breaker_id)
        if breaker is None:
            return

        if success:
            old_state = breaker.state
            breaker.close()
            self._on_transition(breaker, old_state, "closed")
        else:
            old_state = breaker.state
            breaker.reopen()
            self._on_transition(breaker, old_state, "open")

    def reset(self, breaker_id: str) -> None:
        breaker = self._breakers.get(breaker_id)
        if breaker is None:
            return
        old_state = breaker.state
        breaker.close()
        self._on_transition(breaker, old_state, "closed")

    def enable_adaptive(
        self, sensitivity: float = 2.0, min_samples: int = 10, baseline_window: int = 100
    ) -> None:
        self._adaptive_enabled = True
        self._sensitivity = sensitivity
        self._min_samples = min_samples
        self._baseline_window = baseline_window

    def set_recovery_mode(self, breaker_id: str, mode: str) -> None:
        breaker = self._breakers.get(breaker_id)
        if breaker is not None:
            breaker.config = BreakerConfig(
                metric=breaker.config.metric,
                threshold=breaker.config.threshold,
                recovery_mode=mode,
                recovery_after_seconds=breaker.config.recovery_after_seconds,
            )

    def on_state_change(self, callback: Callable[..., object]) -> None:
        self._on_state_change = callback

    def get_metrics(self) -> list[MetricSummary]:
        results = []
        for breaker in self._breakers.values():
            adaptive_threshold = None
            baseline_mean = None
            baseline_stddev = None
            sample_count = 0

            if self._adaptive_enabled:
                samples = self._extract_samples(breaker.config.metric)
                sample_count = len(samples)
                if sample_count >= self._min_samples:
                    baseline_mean = sum(samples) / sample_count
                    variance = sum((x - baseline_mean) ** 2 for x in samples) / sample_count
                    baseline_stddev = math.sqrt(variance)
                    adaptive_threshold = baseline_mean + self._sensitivity * baseline_stddev
                    floor = breaker.config.threshold * 0.5
                    adaptive_threshold = max(adaptive_threshold, floor)

            results.append(
                MetricSummary(
                    metric=breaker.config.metric,
                    current_value=breaker.counter,
                    threshold=breaker.config.threshold,
                    adaptive_threshold=adaptive_threshold,
                    baseline_mean=baseline_mean,
                    baseline_stddev=baseline_stddev,
                    sample_count=sample_count,
                )
            )
        return results

    def restore_state(self) -> None:
        if self._storage is None:
            return
        for breaker in self._breakers.values():
            try:
                state = self._storage.get_breaker_state(breaker.breaker_id)
                if state is not None:
                    breaker.state = state.state
                    if state.tripped_at is not None:
                        breaker.tripped_at = self._clock()
                    breaker.counter = state.metrics.get("counter", 0.0)
            except Exception as e:
                log.warning(
                    "[Ostiari] Failed to restore breaker '%s': %s", breaker.breaker_id, e
                )

    @property
    def breaker_count(self) -> int:
        return len(self._breakers)

    def _effective_threshold(self, breaker: BreakerInstance) -> float:
        if not self._adaptive_enabled:
            return breaker.config.threshold

        samples = self._extract_samples(breaker.config.metric)

        if len(samples) < self._min_samples:
            return breaker.config.threshold

        mean = sum(samples) / len(samples)
        variance = sum((x - mean) ** 2 for x in samples) / len(samples)
        stddev = math.sqrt(variance)

        adaptive = mean + self._sensitivity * stddev
        floor = breaker.config.threshold * 0.5
        return max(adaptive, floor)

    def _extract_samples(self, metric: MetricType) -> list[float]:
        if self._tracer is None:
            return []
        history = self._tracer.recent_history(self._baseline_window)
        if metric == MetricType.WALL_CLOCK_MS:
            return [t.duration_ms for t in history]
        elif metric == MetricType.TOKEN_COST:
            return [
                t.metadata.get("token_cost", 0)
                for t in history
                if t.metadata and "token_cost" in t.metadata
            ]
        elif metric == MetricType.TOTAL_ACTIONS:
            return [1.0] * len(history)
        elif metric == MetricType.ERROR_COUNT:
            return [1.0 if t.tier == "block" else 0.0 for t in history]
        return []

    def _raise_tripped(self, breaker: BreakerInstance) -> None:
        if breaker.config.recovery_mode == "terminate":
            raise AgentTerminatedError(
                breaker_id=breaker.breaker_id,
                reason=f"{breaker.config.metric.value} ({breaker.counter}) exceeded threshold ({breaker.config.threshold})",
            )
        raise BreakerTrippedError(
            breaker_id=breaker.breaker_id,
            metric=breaker.config.metric.value,
            current_value=breaker.counter,
            threshold=breaker.config.threshold,
            recovery_mode=breaker.config.recovery_mode,
        )

    def _on_transition(self, breaker: BreakerInstance, old_state: str, new_state: str) -> None:
        log.info(
            "[Ostiari] breaker '%s' %s -> %s (metric=%s, counter=%.1f, threshold=%.1f)",
            breaker.breaker_id,
            old_state,
            new_state,
            breaker.config.metric.value,
            breaker.counter,
            breaker.config.threshold,
        )

        if self._persist_queue is not None:
            state = BreakerState(
                breaker_id=breaker.breaker_id,
                state=new_state,
                tripped_at=datetime.now(timezone.utc) if breaker.tripped_at else None,
                last_checked=datetime.now(timezone.utc),
                metrics={"counter": breaker.counter},
                recovery_mode=breaker.config.recovery_mode,
                recovery_after_seconds=breaker.config.recovery_after_seconds,
            )
            self._persist_queue.append(("breaker", state))

        if self._on_state_change is not None:
            try:
                self._on_state_change(breaker.breaker_id, old_state, new_state, breaker.counter)
            except Exception as e:
                log.warning("[Ostiari] on_state_change callback failed: %s", e)
