"""Provider health tracking with cooldown periods and latency recording."""

import time


class ProviderHealthTracker:
    """Tracks provider health and manages cooldown periods.

    All state is in-memory. Uses time.time() for timestamps to allow
    easy monkeypatching in tests.
    """

    def __init__(self) -> None:
        # provider -> expiry timestamp (when cooldown ends)
        self._unhealthy: dict[str, float] = {}
        # provider -> list of (timestamp, latency_ms) tuples
        self._latencies: dict[str, list[tuple[float, float]]] = {}

    def mark_unhealthy(self, provider: str, cooldown_seconds: int) -> None:
        """Mark provider as unhealthy with a cooldown period."""
        self._unhealthy[provider] = time.time() + cooldown_seconds

    def is_healthy(self, provider: str) -> bool:
        """Check if provider is healthy (not in cooldown)."""
        expiry = self._unhealthy.get(provider)
        if expiry is None:
            return True
        if time.time() >= expiry:
            # Cooldown expired — clean up and report healthy
            del self._unhealthy[provider]
            return True
        return False

    MAX_LATENCY_RECORDS = 1000

    def record_latency(self, provider: str, latency_ms: float) -> None:
        """Record a latency observation for least-latency routing."""
        if provider not in self._latencies:
            self._latencies[provider] = []
        records = self._latencies[provider]
        records.append((time.time(), latency_ms))
        if len(records) > self.MAX_LATENCY_RECORDS:
            self._latencies[provider] = records[-(self.MAX_LATENCY_RECORDS // 2):]

    def get_average_latency(self, provider: str, window_seconds: int) -> float:
        """Get average latency over the sliding window.

        Returns float('inf') if no records exist within the window.
        """
        records = self._latencies.get(provider)
        if not records:
            return float("inf")

        cutoff = time.time() - window_seconds
        in_window = [lat for ts, lat in records if ts >= cutoff]

        if not in_window:
            return float("inf")

        return sum(in_window) / len(in_window)
