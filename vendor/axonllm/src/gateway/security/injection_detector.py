"""Prompt injection detection — heuristic + pattern-based.

Detects common prompt injection techniques:
- Role-override attempts ("ignore previous instructions", "you are now...")
- System prompt extraction ("repeat your instructions", "what is your system prompt")
- Delimiter escape attempts (closing markup/code blocks to inject new context)
- Encoded payloads (base64-encoded instructions)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum


class ThreatLevel(Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class DetectionResult:
    """Result of prompt injection analysis."""

    threat_level: ThreatLevel
    detected_patterns: list[str] = field(default_factory=list)
    score: float = 0.0
    should_block: bool = False


INJECTION_PATTERNS: list[tuple[str, re.Pattern, float]] = [
    # Role override
    ("role_override", re.compile(
        r"(?i)(ignore|forget|disregard)\s+(all\s+)?(previous|prior|above|earlier)\s+"
        r"(instructions|rules|guidelines|prompts|context)"
    ), 0.9),
    ("role_override", re.compile(
        r"(?i)you\s+are\s+now\s+(a|an|the)\s+\w+"
    ), 0.7),
    ("role_override", re.compile(
        r"(?i)(new\s+instructions|override\s+mode|admin\s+mode|developer\s+mode|DAN\s+mode)"
    ), 0.8),

    # System prompt extraction
    ("extraction", re.compile(
        r"(?i)(repeat|show|display|reveal|print|output)\s+(your|the|all)?\s*"
        r"(system\s+prompt|instructions|rules|initial\s+prompt|hidden\s+prompt)"
    ), 0.85),
    ("extraction", re.compile(
        r"(?i)what\s+(are|is|were)\s+(your|the)\s+"
        r"(instructions|rules|system\s+prompt|initial\s+prompt)"
    ), 0.7),

    # Delimiter escape
    ("delimiter_escape", re.compile(
        r"```\s*\n?\s*(system|assistant|ignore|new instructions)"
    ), 0.75),
    ("delimiter_escape", re.compile(
        r"</?(?:system|instructions|context|rules)>"
    ), 0.6),

    # Encoded payloads
    ("encoded_payload", re.compile(
        r"(?i)(base64|decode|eval|exec)\s*[\(\[{:]\s*[A-Za-z0-9+/=]{20,}"
    ), 0.8),

    # Separator/boundary injection
    ("boundary_injection", re.compile(
        r"[-=]{10,}\s*(system|instructions|new context|admin)"
    ), 0.65),
]

THREAT_THRESHOLDS = {
    ThreatLevel.LOW: 0.3,
    ThreatLevel.MEDIUM: 0.5,
    ThreatLevel.HIGH: 0.7,
    ThreatLevel.CRITICAL: 0.9,
}


class PromptInjectionDetector:
    """Detects prompt injection attempts using pattern matching and scoring.

    Configurable blocking threshold — defaults to HIGH (0.7).
    """

    def __init__(self, block_threshold: float = 0.7) -> None:
        self._block_threshold = block_threshold

    def _normalize(self, text: str) -> str:
        """Normalize Unicode to defeat homoglyph and zero-width char bypass."""
        text = unicodedata.normalize("NFKD", text)
        text = re.sub(r"[​-‏ - ⁠﻿]", "", text)
        return text

    def analyze(self, text: str) -> DetectionResult:
        """Analyze text for prompt injection patterns."""
        normalized = self._normalize(text)
        detected: list[str] = []
        max_score = 0.0

        for pattern_name, regex, weight in INJECTION_PATTERNS:
            if regex.search(normalized):
                detected.append(pattern_name)
                max_score = max(max_score, weight)

        threat_level = ThreatLevel.NONE
        for level in (ThreatLevel.CRITICAL, ThreatLevel.HIGH, ThreatLevel.MEDIUM, ThreatLevel.LOW):
            if max_score >= THREAT_THRESHOLDS[level]:
                threat_level = level
                break

        return DetectionResult(
            threat_level=threat_level,
            detected_patterns=list(set(detected)),
            score=max_score,
            should_block=max_score >= self._block_threshold,
        )

    def analyze_messages(self, messages: list[dict]) -> DetectionResult:
        """Analyze all messages in a conversation for injection attempts."""
        combined_detected: list[str] = []
        max_score = 0.0

        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue
            result = self.analyze(content)
            combined_detected.extend(result.detected_patterns)
            max_score = max(max_score, result.score)

        threat_level = ThreatLevel.NONE
        for level in (ThreatLevel.CRITICAL, ThreatLevel.HIGH, ThreatLevel.MEDIUM, ThreatLevel.LOW):
            if max_score >= THREAT_THRESHOLDS[level]:
                threat_level = level
                break

        return DetectionResult(
            threat_level=threat_level,
            detected_patterns=list(set(combined_detected)),
            score=max_score,
            should_block=max_score >= self._block_threshold,
        )
