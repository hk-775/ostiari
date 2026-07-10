"""LoopDetector — detect exact and similar action repetition patterns."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

from ostiari.models import AnomalySignal, TraceEntry


class LoopDetector:
    def __init__(
        self,
        threshold: int = 3,
        similarity_threshold: float = 0.6,
        separation_threshold: int = 3,
    ) -> None:
        self._threshold = threshold
        self._similarity_threshold = similarity_threshold
        self._separation_threshold = separation_threshold

    @property
    def name(self) -> str:
        return "loop"

    def detect(
        self,
        action: str,
        params: dict[str, Any],
        history: list[TraceEntry],
    ) -> AnomalySignal | None:
        if not history:
            return None

        if self._is_separated(action, history):
            return None

        exact_count = self._count_exact(action, params, history)
        if exact_count >= self._threshold:
            return AnomalySignal(
                detector="loop",
                severity=self._severity_for_count(exact_count),
                score_contribution=min(exact_count * 10, 80),
                description=f"Loop detected: '{action}' called {exact_count} times (exact match)",
                evidence={
                    "repeated_action": action,
                    "count": exact_count,
                    "window_size": len(history),
                    "similarity_type": "exact",
                    "param_keys": list(params.keys()),
                },
            )

        similar_count = self._count_similar(action, params, history)
        if similar_count >= self._threshold:
            return AnomalySignal(
                detector="loop",
                severity="medium",
                score_contribution=min(similar_count * 8, 60),
                description=f"Near-loop detected: '{action}' called {similar_count} times with similar params",
                evidence={
                    "repeated_action": action,
                    "count": similar_count,
                    "window_size": len(history),
                    "similarity_type": "similar",
                    "param_keys": list(params.keys()),
                },
            )

        return None

    def _count_exact(self, action: str, params: dict[str, Any], history: list[TraceEntry]) -> int:
        return sum(1 for entry in history if entry.action == action and entry.params == params)

    def _count_similar(self, action: str, params: dict[str, Any], history: list[TraceEntry]) -> int:
        current_tokens = _extract_tokens(params)
        count = 0
        for entry in history:
            if entry.action != action:
                continue
            entry_tokens = _extract_tokens(entry.params)
            if _jaccard(current_tokens, entry_tokens) >= self._similarity_threshold:
                count += 1
        return count

    def _is_separated(self, action: str, history: list[TraceEntry]) -> bool:
        last_index = None
        for i in range(len(history) - 1, -1, -1):
            if history[i].action == action:
                last_index = i
                break

        if last_index is None:
            return True

        distinct_between = set()
        for i in range(last_index + 1, len(history)):
            if history[i].action != action:
                distinct_between.add(history[i].action)

        return len(distinct_between) >= self._separation_threshold

    def _severity_for_count(self, count: int) -> str:
        if count >= 10:
            return "critical"
        if count >= 6:
            return "high"
        return "medium"


def _extract_tokens(params: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for value in _flatten_strings(params):
        tokens.update(value.lower().split())
    return tokens


def _flatten_strings(obj: Any) -> Generator[str, None, None]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten_strings(v)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _flatten_strings(item)


def _jaccard(tokens_a: set[str], tokens_b: set[str]) -> float:
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
