"""Security layer — prompt-injection detection and PII redaction.

Wraps AxonLLM's security engine (``src.gateway.security.*``) to provide:
- Prompt-injection detection and blocking
- PII redaction before prompts reach the LLM (reversible)

FAIL-CLOSED: for a governance gateway, "requested but the detector isn't
available" and "the detector raised" must **block**, not silently allow. This
module previously swallowed ImportError and returned allow/unredacted on any
error — see the technical assessment (B3). It now surfaces those as a block.
"""

import logging
from typing import Any

log = logging.getLogger("ostiari.sidecar.llm.security")


class SecurityUnavailableError(RuntimeError):
    """A security control was enabled but its engine could not be initialized."""


class SecurityLayer:
    """Pre-processes messages for security before sending to LLM."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._pii_redactor: Any = None
        self._injection_detector: Any = None
        self._pii_enabled = self._config.get("pii_redaction", False)
        self._injection_enabled = self._config.get("injection_detection", False)
        # Records why a control is unavailable so process_messages can fail closed.
        self._pii_unavailable: str = ""
        self._injection_unavailable: str = ""
        self._init_security()

    def _init_security(self) -> None:
        """Initialize AxonLLM security components for the enabled controls.

        If a control is enabled but its engine can't load, we DON'T disable it
        silently — we remember it's unavailable and fail closed at call time.
        """
        threshold = float(self._config.get("injection_threshold", 0.7))

        if self._pii_enabled:
            try:
                from src.gateway.security.pii_redactor import PIIRedactor

                self._pii_redactor = PIIRedactor()
                log.info("PII redaction enabled")
            except Exception as e:  # noqa: BLE001
                self._pii_unavailable = f"PII redactor unavailable: {e}"
                log.error("PII redaction ENABLED but engine unavailable — will fail closed: %s", e)

        if self._injection_enabled:
            try:
                from src.gateway.security.injection_detector import PromptInjectionDetector

                self._injection_detector = PromptInjectionDetector(block_threshold=threshold)
                log.info("Prompt-injection detection enabled (threshold=%.2f)", threshold)
            except Exception as e:  # noqa: BLE001
                self._injection_unavailable = f"injection detector unavailable: {e}"
                log.error("Injection detection ENABLED but engine unavailable — will fail closed: %s", e)

    def process_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Run the security pipeline over messages.

        Returns (processed_messages, metadata). metadata always carries
        ``blocked`` (bool) and ``block_reason`` (str); when blocked the caller
        MUST reject the request. Injection detection blocks; PII redaction
        rewrites content (and returns a ``redaction_map`` for response
        re-injection). Both fail closed: if an enabled control is unavailable or
        errors, the request is blocked.
        """
        metadata: dict[str, Any] = {
            "pii_redacted": False,
            "injection_detected": False,
            "blocked": False,
            "block_reason": "",
        }

        # ── Injection detection (fail-closed) ────────────────────────────
        if self._injection_enabled:
            if self._injection_detector is None:
                metadata["blocked"] = True
                metadata["block_reason"] = self._injection_unavailable or "injection detector unavailable"
                metadata["injection_detected"] = True
                return messages, metadata
            try:
                result = self._injection_detector.analyze_messages(list(messages))
                score = float(getattr(result, "score", 0.0) or 0.0)
                metadata["injection_score"] = score
                if getattr(result, "should_block", False):
                    metadata["injection_detected"] = True
                    metadata["blocked"] = True
                    patterns = getattr(result, "detected_patterns", []) or []
                    metadata["block_reason"] = (
                        f"prompt injection detected (score {score:.2f}"
                        + (f", patterns: {', '.join(patterns)}" if patterns else "") + ")"
                    )
                    return messages, metadata
            except Exception as e:  # noqa: BLE001 — fail CLOSED
                log.error("Injection detection errored — failing closed: %s", e)
                metadata["blocked"] = True
                metadata["injection_detected"] = True
                metadata["block_reason"] = f"injection detection error: {e}"
                return messages, metadata

        # ── PII redaction (fail-closed on error when enabled) ────────────
        if self._pii_enabled:
            if self._pii_redactor is None:
                metadata["blocked"] = True
                metadata["block_reason"] = self._pii_unavailable or "PII redactor unavailable"
                return messages, metadata
            try:
                messages, redaction_map = self._redact_pii(messages)
                if redaction_map:
                    metadata["pii_redacted"] = True
                    metadata["redaction_map"] = redaction_map
            except Exception as e:  # noqa: BLE001 — fail CLOSED (don't leak PII)
                log.error("PII redaction errored — failing closed: %s", e)
                metadata["blocked"] = True
                metadata["block_reason"] = f"PII redaction error: {e}"
                return messages, metadata

        return messages, metadata

    def restore_pii(self, text: str, redaction_map: dict[str, str]) -> str:
        """Restore redacted PII tokens in response text."""
        for token, original in redaction_map.items():
            text = text.replace(token, original)
        return text

    def _policy(self) -> Any:
        """Build a ResolvedPolicy enabling redaction for all known PII types."""
        from src.gateway.models import ResolvedPolicy
        from src.gateway.security.pii_redactor import PII_PATTERNS

        types = self._config.get("pii_redact_types") or list(PII_PATTERNS.keys())
        return ResolvedPolicy(pii_redaction_enabled=True, pii_redact_types=types)

    def _redact_pii(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Redact PII from message content using AxonLLM's PIIRedactor.

        Uses the real ``redact_messages(messages, policy)`` API. Any failure
        propagates so process_messages can fail closed (no silent PII leak).
        """
        policy = self._policy()
        processed, mapping = self._pii_redactor.redact_messages(list(messages), policy)
        redaction_map = dict(getattr(mapping, "_forward", {}) or {})
        return processed, redaction_map
