"""CustomDetector protocol for anomaly detection plugins."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ostiari.models import AnomalySignal, TraceEntry


@runtime_checkable
class CustomDetector(Protocol):
    @property
    def name(self) -> str: ...

    def detect(
        self,
        action: str,
        params: dict[str, Any],
        history: list[TraceEntry],
    ) -> AnomalySignal | None: ...
