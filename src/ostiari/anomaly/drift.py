"""DriftDetector — detect actions outside defined scope boundaries."""

from __future__ import annotations

import fnmatch
from typing import Any

from ostiari.models import AnomalySignal, TraceEntry


class DriftDetector:
    def __init__(self, orchestrator: Any) -> None:
        self._orchestrator = orchestrator

    @property
    def name(self) -> str:
        return "drift"

    def detect(
        self,
        action: str,
        params: dict[str, Any],
        history: list[TraceEntry],
    ) -> AnomalySignal | None:
        scope = self._orchestrator._scope
        if scope is None:
            return None

        patterns = scope.allowed_patterns
        if self._matches_scope(action, patterns):
            return None

        if self._check_progressive(action, history, patterns):
            return AnomalySignal(
                detector="drift",
                severity="critical",
                score_contribution=80,
                description=f"Progressive drift detected: '{action}' and multiple recent actions are outside scope",
                evidence={
                    "action": action,
                    "allowed_patterns": patterns,
                    "drift_type": "progressive",
                    "recent_actions": [e.action for e in history[-5:]],
                },
            )

        return AnomalySignal(
            detector="drift",
            severity="high",
            score_contribution=60,
            description=f"Scope violation: '{action}' is outside defined scope",
            evidence={
                "action": action,
                "allowed_patterns": patterns,
                "drift_type": "scope_violation",
                "recent_actions": [e.action for e in history[-5:]],
            },
        )

    def _matches_scope(self, action: str, patterns: list[str]) -> bool:
        return any(fnmatch.fnmatch(action, p) for p in patterns)

    def _check_progressive(
        self, action: str, history: list[TraceEntry], patterns: list[str]
    ) -> bool:
        recent = history[-4:]
        out_of_scope = sum(1 for entry in recent if not self._matches_scope(entry.action, patterns))
        # Current action is also out-of-scope (we already checked), so total is out_of_scope + 1
        return (out_of_scope + 1) >= 3
