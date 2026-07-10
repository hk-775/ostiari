"""HallucinationDetector — detect tool calls not in registered inventory."""

from __future__ import annotations

from typing import Any

from ostiari.models import AnomalySignal, TraceEntry

try:
    from rapidfuzz.fuzz import ratio as _fuzz_ratio

    def similarity_ratio(a: str, b: str) -> float:
        score: float = _fuzz_ratio(a, b) / 100.0
        return score

except ImportError:
    from difflib import SequenceMatcher

    def similarity_ratio(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()


class HallucinationDetector:
    def __init__(self, inventory_ref: dict[str, Any]) -> None:
        self._inventory = inventory_ref

    @property
    def name(self) -> str:
        return "hallucination"

    def detect(
        self,
        action: str,
        params: dict[str, Any],
        history: list[TraceEntry],
    ) -> AnomalySignal | None:
        if not self._inventory:
            return None

        if action in self._inventory:
            return None

        best_name, best_ratio = self._find_best_match(action)
        suggestion = f"Did you mean '{best_name}'?" if best_ratio >= 0.7 else None

        return AnomalySignal(
            detector="hallucination",
            severity="high",
            score_contribution=70,
            description=f"Unknown tool '{action}' not in registered inventory",
            evidence={
                "attempted_tool": action,
                "inventory_size": len(self._inventory),
                "suggestion": suggestion,
                "similarity_score": round(best_ratio, 3) if suggestion else None,
            },
        )

    def _find_best_match(self, action: str) -> tuple[str | None, float]:
        best_name: str | None = None
        best_ratio = 0.0
        for tool_name in self._inventory:
            ratio = similarity_ratio(action, tool_name)
            if ratio > best_ratio:
                best_ratio = ratio
                best_name = tool_name
        return best_name, best_ratio
