"""Security layer — prompt-injection detection and PII redaction.

Backed by ``ostiari.detect``, Ostiari's own detection engine.

This used to wrap AxonLLM's private ``src.gateway.security.*`` modules. Ostiari
now bundles AxonLLM for routing, but content controls remain an Ostiari-owned
host responsibility and use the stable ``ostiari.detect`` API. Keeping that
boundary prevents routing-library internals from becoming part of Ostiari's
security contract.

``ostiari.detect`` is part of the ``ostiari`` package, which is a hard dependency
of this gateway (see ``pyproject.toml``), so the engine is always importable and
these controls actually work when switched on. The fail-closed machinery below is
kept — an engine that errors at call time still blocks — but it is now the
genuine edge case it was meant to be rather than the default state.

FAIL-CLOSED: for a governance gateway, "requested but the detector isn't
available" and "the detector raised" must **block**, not silently allow — see the
technical assessment (B3).

Defaults are unchanged: both controls are **off** unless the pushed config turns
them on. Enabling detection by default is a policy decision for the operator,
not a side effect of making detection work.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("ostiari.sidecar.llm.security")


class SecurityUnavailableError(RuntimeError):
    """A security control was enabled but its engine could not be initialized."""


class SecurityLayer:
    """Pre-processes messages for security before sending to LLM.

    Config keys (all optional, all off by default):

    ``pii_redaction``       bool  — redact PII before the prompt leaves the process
    ``pii_redact_types``    list  — restrict to specific types (default: all)
    ``pii_reversible``      bool  — retain originals to restore in the response
                                    (default True; False keeps no plaintext)
    ``injection_detection`` bool  — scan for prompt injection
    ``injection_threshold`` float — score at or above which a request is blocked
    ``injection_mode``      str   — ``block`` (default) or ``flag``. ``flag``
                                    scores and reports but does not reject, for
                                    tuning thresholds against real traffic
                                    before turning enforcement on.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._pii_redactor: Any = None
        self._injection_detector: Any = None
        self._pii_enabled = bool(self._config.get("pii_redaction", False))
        self._injection_enabled = bool(self._config.get("injection_detection", False))
        self._pii_reversible = bool(self._config.get("pii_reversible", True))
        self._injection_mode = str(self._config.get("injection_mode", "block")).lower()
        # Records why a control is unavailable so process_messages can fail closed.
        self._pii_unavailable: str = ""
        self._injection_unavailable: str = ""
        self._init_security()

    def _init_security(self) -> None:
        """Initialize the detection engines for the enabled controls.

        If a control is enabled but its engine can't load, we DON'T disable it
        silently — we remember it's unavailable and fail closed at call time.
        """
        threshold = float(self._config.get("injection_threshold", 0.7))

        if self._pii_enabled:
            try:
                from ostiari.detect import PIIRedactor

                self._pii_redactor = PIIRedactor(types=self._config.get("pii_redact_types"))
                log.info(
                    "PII redaction enabled (types=%s, reversible=%s)",
                    self._config.get("pii_redact_types") or "all",
                    self._pii_reversible,
                )
            except Exception as e:  # noqa: BLE001
                self._pii_unavailable = f"PII redactor unavailable: {e}"
                log.error("PII redaction ENABLED but engine unavailable — will fail closed: %s", e)

        if self._injection_enabled:
            try:
                from ostiari.detect import InjectionDetector

                self._injection_detector = InjectionDetector(block_threshold=threshold)
                log.info(
                    "Prompt-injection detection enabled (threshold=%.2f, mode=%s)",
                    threshold, self._injection_mode,
                )
            except Exception as e:  # noqa: BLE001
                self._injection_unavailable = f"injection detector unavailable: {e}"
                log.error(
                    "Injection detection ENABLED but engine unavailable — will fail closed: %s", e
                )

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
                metadata["block_reason"] = (
                    self._injection_unavailable or "injection detector unavailable"
                )
                metadata["injection_detected"] = True
                return messages, metadata
            try:
                result = self._injection_detector.analyze_messages(list(messages))
                score = float(getattr(result, "score", 0.0) or 0.0)
                patterns = list(
                    getattr(result, "matched_patterns", None)
                    # AxonLLM's engine named this field differently; tolerate both
                    # so a hand-injected detector (tests, a custom engine) works.
                    or getattr(result, "detected_patterns", None)
                    or []
                )
                metadata["injection_score"] = score
                if patterns:
                    metadata["injection_patterns"] = patterns
                if getattr(result, "should_block", False):
                    metadata["injection_detected"] = True
                    reason = (
                        f"prompt injection detected (score {score:.2f}"
                        + (f", patterns: {', '.join(patterns)}" if patterns else "")
                        + ")"
                    )
                    if self._injection_mode == "flag":
                        # Observe-only: score it, report it, let it through. For
                        # measuring a threshold against real traffic before
                        # switching enforcement on.
                        metadata["injection_flagged"] = True
                        log.warning("Injection FLAGGED (not blocked): %s", reason)
                    else:
                        metadata["blocked"] = True
                        metadata["block_reason"] = reason
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

    def _redact_pii(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        """Redact PII from message content.

        Any failure propagates so process_messages can fail closed (no silent
        PII leak). ``forward`` is token → original, which is exactly the shape
        ``restore_pii`` consumes; with ``pii_reversible=False`` it is empty and
        nothing is restorable, by design.
        """
        processed, mapping = self._pii_redactor.redact_messages(
            list(messages), reversible=self._pii_reversible
        )
        forward = getattr(mapping, "forward", None)
        if forward is None:  # a foreign engine (AxonLLM) exposes it privately
            forward = getattr(mapping, "_forward", {})
        return processed, dict(forward or {})
