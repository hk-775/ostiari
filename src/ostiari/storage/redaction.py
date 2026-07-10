"""Field redaction filter for sensitive data before storage."""

from __future__ import annotations

import fnmatch
from typing import Any

DEFAULT_REDACT_PATTERNS = [
    "*password*",
    "*secret*",
    "*token*",
    "*key*",
    "*credential*",
]

REDACTED_VALUE = "[REDACTED]"


class RedactionFilter:
    """Recursively redacts sensitive fields from data structures."""

    def __init__(self, patterns: list[str] | None = None) -> None:
        all_patterns = DEFAULT_REDACT_PATTERNS + (patterns or [])
        self._patterns = [p.lower() for p in all_patterns]

    def redact(self, data: Any) -> Any:
        if isinstance(data, dict):
            return {
                k: REDACTED_VALUE if self._matches(k) else self.redact(v) for k, v in data.items()
            }
        if isinstance(data, list):
            return [self.redact(item) for item in data]
        return data

    def _matches(self, key: str) -> bool:
        key_lower = key.lower()
        return any(fnmatch.fnmatch(key_lower, pattern) for pattern in self._patterns)
