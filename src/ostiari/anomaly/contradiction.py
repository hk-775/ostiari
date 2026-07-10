"""ContradictionDetector — compare claimed vs actual results."""

from __future__ import annotations

from typing import Any

from ostiari.models import AnomalySignal, TraceEntry


class ContradictionDetector:
    @property
    def name(self) -> str:
        return "contradiction"

    def detect(
        self,
        action: str,
        params: dict[str, Any],
        history: list[TraceEntry],
    ) -> AnomalySignal | None:
        if "expected_result" not in params:
            return None

        prior_result = self._find_prior_result(action, history)
        if prior_result is None:
            return None

        expected = params["expected_result"]
        mismatch = self._find_mismatch(expected, prior_result)
        if mismatch is None:
            return None

        field, expected_val, actual_val = mismatch
        return AnomalySignal(
            detector="contradiction",
            severity="medium",
            score_contribution=40,
            description=f"Contradiction: expected {field}={expected_val!r}, got {actual_val!r}",
            evidence={
                "action": action,
                "claimed": expected,
                "actual": prior_result,
                "field": field,
            },
        )

    def _find_prior_result(self, action: str, history: list[TraceEntry]) -> Any | None:
        for entry in reversed(history):
            if entry.action == action and entry.result is not None:
                return entry.result
        return None

    def _find_mismatch(self, expected: Any, actual: Any) -> tuple[str, Any, Any] | None:
        if isinstance(expected, dict) and isinstance(actual, dict):
            for key in expected:
                if key not in actual or expected[key] != actual[key]:
                    return (key, expected[key], actual.get(key))
            return None
        if expected != actual:
            return ("result", expected, actual)
        return None
