"""Security layer — PII redaction and prompt injection detection via AxonLLM.

Wraps AxonLLM's security modules to provide:
- PII redaction before prompts reach the LLM (reversible)
- Prompt injection detection and blocking
"""

import logging
from typing import Any

log = logging.getLogger("ostiari.sidecar.llm.security")


class SecurityLayer:
    """Pre-processes messages for security before sending to LLM."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._pii_redactor: Any = None
        self._injection_detector: Any = None
        self._pii_enabled = self._config.get("pii_redaction", False)
        self._injection_enabled = self._config.get("injection_detection", False)
        self._init_security()

    def _init_security(self) -> None:
        """Initialize AxonLLM security components if available."""
        if self._pii_enabled:
            try:
                from gateway.security.pii_redactor import PIIRedactor

                self._pii_redactor = PIIRedactor()
                log.info("PII redaction enabled (powered by AxonLLM)")
            except ImportError:
                log.warning("PII redaction requested but gateway.security not available")

        if self._injection_enabled:
            try:
                from gateway.security.injection_detector import PromptInjectionDetector

                self._injection_detector = PromptInjectionDetector()
                log.info("Prompt injection detection enabled (powered by AxonLLM)")
            except ImportError:
                log.warning("Injection detection requested but gateway.security not available")

    def process_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Process messages through security pipeline.

        Returns:
            Tuple of (processed_messages, security_metadata).
            security_metadata includes redaction mappings and injection scores.
        """
        metadata: dict[str, Any] = {
            "pii_redacted": False,
            "injection_detected": False,
        }

        # PII Redaction
        if self._pii_redactor is not None:
            messages, redaction_map = self._redact_pii(messages)
            if redaction_map:
                metadata["pii_redacted"] = True
                metadata["redaction_map"] = redaction_map

        # Injection Detection
        if self._injection_detector is not None:
            is_injection, score = self._detect_injection(messages)
            metadata["injection_score"] = score
            if is_injection:
                metadata["injection_detected"] = True

        return messages, metadata

    def restore_pii(self, text: str, redaction_map: dict[str, str]) -> str:
        """Restore redacted PII tokens in response text."""
        for token, original in redaction_map.items():
            text = text.replace(token, original)
        return text

    def _redact_pii(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Redact PII from message content using AxonLLM's PIIRedactor."""
        redaction_map: dict[str, str] = {}

        try:
            from gateway.security.pii_redactor import RedactionMapping

            mapping = RedactionMapping()
            processed = []
            for msg in messages:
                content = msg.get("content", "")
                if content and isinstance(content, str):
                    redacted = self._pii_redactor.redact(content, mapping)
                    processed.append({**msg, "content": redacted})
                else:
                    processed.append(msg)

            redaction_map = dict(mapping._forward) if mapping._forward else {}
            return processed, redaction_map
        except Exception as e:
            log.warning("PII redaction failed: %s", e)
            return messages, {}

    def _detect_injection(self, messages: list[dict[str, Any]]) -> tuple[bool, float]:
        """Check messages for prompt injection attempts."""
        try:
            combined_text = " ".join(
                msg.get("content", "") for msg in messages
                if isinstance(msg.get("content"), str)
            )
            result = self._injection_detector.detect(combined_text)
            threshold = self._config.get("injection_threshold", 0.7)
            return result.score >= threshold, result.score
        except Exception as e:
            log.warning("Injection detection failed: %s", e)
            return False, 0.0
