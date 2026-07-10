"""AnomalyDetector — orchestrates built-in and custom anomaly detectors."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ostiari.anomaly.contradiction import ContradictionDetector
from ostiari.anomaly.drift import DriftDetector
from ostiari.anomaly.hallucination import HallucinationDetector
from ostiari.anomaly.loop import LoopDetector
from ostiari.anomaly.protocol import CustomDetector
from ostiari.models import AnomalySignal, TraceEntry

logger = logging.getLogger("ostiari")


@dataclass
class ToolEntry:
    name: str
    schema: dict[str, Any] | None = None
    registered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class DetectionScope:
    allowed_patterns: list[str]
    description: str | None = None


class AnomalyDetector:
    """Orchestrates anomaly detection across built-in and custom detectors."""

    def __init__(
        self,
        loop_threshold: int = 3,
        loop_window: int = 20,
        similarity_threshold: float = 0.6,
        separation_threshold: int = 3,
    ) -> None:
        self._inventory: dict[str, ToolEntry] = {}
        self._scope: DetectionScope | None = None
        self._loop_window = loop_window
        self._custom_detectors: list[CustomDetector] = []

        self._loop_detector = LoopDetector(
            threshold=loop_threshold,
            similarity_threshold=similarity_threshold,
            separation_threshold=separation_threshold,
        )
        self._hallucination_detector = HallucinationDetector(inventory_ref=self._inventory)
        self._drift_detector = DriftDetector(orchestrator=self)
        self._contradiction_detector = ContradictionDetector()

    def analyze(
        self, action: str, params: dict[str, Any], history: list[TraceEntry]
    ) -> list[AnomalySignal]:
        """Run all detectors and collect signals."""
        window = history[-self._loop_window :] if len(history) > self._loop_window else history
        signals: list[AnomalySignal] = []

        detectors: list[CustomDetector] = [
            self._hallucination_detector,
            self._loop_detector,
            self._drift_detector,
            self._contradiction_detector,
            *self._custom_detectors,
        ]

        for detector in detectors:
            try:
                signal = detector.detect(action, params, window)
                if signal is not None:
                    signals.append(signal)
            except Exception as exc:
                logger.warning(
                    "Detector '%s' failed: %s", getattr(detector, "name", "unknown"), exc
                )
                signals.append(
                    AnomalySignal(
                        detector="_error",
                        severity="medium",
                        score_contribution=10,
                        description=f"Detector '{getattr(detector, 'name', 'unknown')}' raised {type(exc).__name__}: {exc}",
                        evidence={
                            "detector": getattr(detector, "name", "unknown"),
                            "error_type": type(exc).__name__,
                        },
                    )
                )

        return signals

    def register_tool(self, name: str, schema: dict[str, Any] | None = None) -> None:
        """Add a tool to the known inventory (copy-on-write for read-safety)."""
        new_inventory = dict(self._inventory)
        new_inventory[name] = ToolEntry(name=name, schema=schema)
        self._inventory = new_inventory
        self._hallucination_detector._inventory = new_inventory

    def unregister_tool(self, name: str) -> None:
        """Remove a tool from the inventory (copy-on-write for read-safety)."""
        new_inventory = dict(self._inventory)
        new_inventory.pop(name, None)
        self._inventory = new_inventory
        self._hallucination_detector._inventory = new_inventory

    def register_custom(self, detector: CustomDetector) -> None:
        """Register a custom detector plugin. Validates protocol compliance."""
        if not isinstance(detector, CustomDetector):
            raise TypeError(
                f"Detector must implement CustomDetector protocol "
                f"(requires 'name' property and 'detect' method), "
                f"got {type(detector).__name__}"
            )
        self._custom_detectors.append(detector)

    def set_scope(self, patterns: list[str]) -> None:
        """Define allowed tool patterns for drift detection."""
        self._scope = DetectionScope(allowed_patterns=patterns)

    def clear_scope(self) -> None:
        """Remove scope definition (disables drift detection)."""
        self._scope = None

    @property
    def inventory_size(self) -> int:
        return len(self._inventory)

    @property
    def scope_patterns(self) -> list[str] | None:
        return self._scope.allowed_patterns if self._scope else None

    @property
    def detector_count(self) -> int:
        return 4 + len(self._custom_detectors)
