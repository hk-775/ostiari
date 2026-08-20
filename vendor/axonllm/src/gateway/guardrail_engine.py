"""Guardrail engine for evaluating requests and responses against configurable rules."""

from __future__ import annotations

import asyncio
import math
from functools import lru_cache

import regex

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    GuardrailResult,
    GuardrailRule,
)

ALLOWED_GUARDRAIL_RULE_TYPES = frozenset(
    {"keyword_block", "regex_match", "content_category"}
)
ALLOWED_GUARDRAIL_ACTIONS = frozenset({"block", "warn", "redact"})
ALLOWED_GUARDRAIL_TARGETS = frozenset({"request", "response", "both"})
DEFAULT_REGEX_TIMEOUT_SECONDS = 0.05
_REGEX_THREAD_GRACE_SECONDS = 0.05
_MAX_REGEX_PATTERN_BYTES = 16 * 1024


@lru_cache(maxsize=1024)
def compile_guardrail_regex(pattern: str) -> regex.Pattern:
    """Compile one bounded guardrail regex or raise a stable validation error."""
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("guardrail regex pattern must be a non-empty string")
    if len(pattern.encode("utf-8")) > _MAX_REGEX_PATTERN_BYTES:
        raise ValueError("guardrail regex pattern exceeds the maximum size")
    try:
        return regex.compile(pattern)
    except regex.error as exc:
        raise ValueError("guardrail regex pattern is invalid") from exc


class GuardrailEngine:
    """Evaluates requests and responses against project guardrail rules."""

    def __init__(
        self,
        *,
        regex_timeout_seconds: float = DEFAULT_REGEX_TIMEOUT_SECONDS,
    ) -> None:
        timeout = float(regex_timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("regex_timeout_seconds must be finite and positive")
        self._regex_timeout_seconds = timeout

    async def evaluate_request(
        self, request: ChatCompletionRequest, rules: list[GuardrailRule]
    ) -> GuardrailResult:
        """Evaluate request against guardrail rules.

        Extracts text from all messages in the request and checks against
        rules where applies_to is "request" or "both".

        Returns GuardrailResult with pass/fail, violated rule names, and message.
        """
        text = self._extract_request_text(request)
        return await self._evaluate(
            text,
            rules,
            context="Request",
            target="request",
        )

    async def evaluate_response(
        self, response: ChatCompletionResponse, rules: list[GuardrailRule]
    ) -> GuardrailResult:
        """Evaluate response against guardrail rules.

        Extracts text from choices[*].message.content and checks against
        rules where applies_to is "response" or "both".

        Returns GuardrailResult with pass/fail, violated rule names, and message.
        """
        text = self._extract_response_text(response)
        return await self._evaluate(
            text,
            rules,
            context="Response",
            target="response",
        )

    def _extract_request_text(self, request: ChatCompletionRequest) -> str:
        """Extract all text content from request messages."""
        parts: list[str] = []
        for msg in request.messages:
            content = msg.get("content", "")
            if content:
                parts.append(str(content))
        return " ".join(parts)

    def _extract_response_text(self, response: ChatCompletionResponse) -> str:
        """Extract text content from response choices."""
        parts: list[str] = []
        for choice in response.choices:
            message = choice.get("message", {})
            content = message.get("content", "")
            if content:
                parts.append(str(content))
        return " ".join(parts)

    async def _evaluate(
        self,
        text: str,
        rules: list[GuardrailRule],
        *,
        context: str,
        target: str,
    ) -> GuardrailResult:
        """Evaluate text against a list of rules.

        Only rules with action="block" cause passed=False.
        "warn" and "redact" rules add to violated_rules but don't fail.
        """
        violated_rules: list[str] = []
        blocking_names: list[str] = []

        for rule in rules:
            name = (
                rule.name
                if isinstance(rule.name, str) and rule.name
                else "invalid_guardrail_rule"
            )
            if not self._valid_rule(rule):
                violated_rules.append(name)
                blocking_names.append(name)
                continue
            if rule.applies_to not in (target, "both"):
                continue
            try:
                matched = await self._matches(text, rule)
            except (TimeoutError, ValueError, regex.error):
                # A malformed or computationally unsafe persisted rule must not
                # silently disable a configured content control.
                violated_rules.append(name)
                blocking_names.append(name)
                continue
            if matched:
                violated_rules.append(name)
                if rule.action == "block":
                    blocking_names.append(name)

        if not violated_rules:
            return GuardrailResult(passed=True, violated_rules=[], message=None)

        passed = not blocking_names
        message: str | None = None
        if not passed:
            message = f"{context} blocked by guardrail rules: {', '.join(blocking_names)}"

        return GuardrailResult(
            passed=passed, violated_rules=violated_rules, message=message
        )

    @staticmethod
    def _valid_rule(rule: GuardrailRule) -> bool:
        return (
            isinstance(rule.name, str)
            and bool(rule.name)
            and rule.rule_type in ALLOWED_GUARDRAIL_RULE_TYPES
            and rule.action in ALLOWED_GUARDRAIL_ACTIONS
            and rule.applies_to in ALLOWED_GUARDRAIL_TARGETS
            and isinstance(rule.pattern, str)
            and bool(rule.pattern)
        )

    async def _matches(self, text: str, rule: GuardrailRule) -> bool:
        """Check if text matches a guardrail rule's pattern."""
        if rule.rule_type == "keyword_block":
            return rule.pattern.lower() in text.lower()
        if rule.rule_type == "regex_match":
            compiled = await asyncio.to_thread(
                compile_guardrail_regex,
                rule.pattern,
            )

            def _search() -> bool:
                return (
                    compiled.search(
                        text,
                        timeout=self._regex_timeout_seconds,
                    )
                    is not None
                )

            return await asyncio.wait_for(
                asyncio.to_thread(_search),
                timeout=(
                    self._regex_timeout_seconds
                    + _REGEX_THREAD_GRACE_SECONDS
                ),
            )
        if rule.rule_type == "content_category":
            # Treat as keyword_block for now (simple implementation)
            return rule.pattern.lower() in text.lower()
        raise ValueError("unsupported guardrail rule type")
