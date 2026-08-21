"""PII redaction engine — policy-driven, configurable per org/BU/project.

Replaces detected PII with indexed tokens ([EMAIL_1], [SSN_2], etc.) before
the prompt reaches the LLM. Stores a reversible mapping so originals can be
re-injected into the response for the caller.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.gateway.models import ResolvedPolicy

logger = logging.getLogger(__name__)

# Regex patterns per PII type — intentionally conservative to reduce false positives
PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    "phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
    "ip_address": re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    "aws_account_id": re.compile(r"\b\d{12}\b"),
    "medical_record": re.compile(r"\b(?:MRN|mrn)[:\s#]*\d{6,10}\b"),
    # International / broader coverage (the set was previously US-centric).
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "passport": re.compile(r"\b[A-Z]{1,2}\d{6,9}\b"),
    "ipv6": re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b"),
}


def env_default_enabled() -> bool:
    """True when AXON_PII_REDACTION_DEFAULT opts a deploy into safe-by-default.

    Backward-compatible: unset → False (redaction stays opt-in via policy). When
    set truthy, any request whose resolved policy does NOT explicitly configure
    redaction gets redaction ON with a default type set. This makes a standalone
    AxonLLM deploy safe-by-default with one env flag, without changing behavior
    for deploys that don't set it (e.g. the Ostiari embed, which governs PII at
    its own layer before the request reaches the embedded agent).
    """
    return os.environ.get("AXON_PII_REDACTION_DEFAULT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def env_default_types() -> list[str]:
    """Default PII types used when the env default is on and policy sets none.

    AXON_PII_REDACT_TYPES (comma-separated) narrows the set; unset → all known
    patterns. Unknown type names are dropped with a warning.
    """
    raw = os.environ.get("AXON_PII_REDACT_TYPES", "").strip()
    if not raw:
        return list(PII_PATTERNS.keys())
    types, unknown = [], []
    for t in (p.strip() for p in raw.split(",")):
        if not t:
            continue
        (types if t in PII_PATTERNS else unknown).append(t)
    if unknown:
        logger.warning("AXON_PII_REDACT_TYPES has unknown types (ignored): %s", unknown)
    return types or list(PII_PATTERNS.keys())


@dataclass
class RedactionMapping:
    """Stores the mapping between tokens and original values for re-injection.

    When ``reversible`` is False (permanent-redaction / no-reinject mode) the
    token→original map is never populated, so no PII plaintext is retained past
    the redaction pass and ``reinject`` is a no-op. Same-request dedup still
    works via ``_dedup`` (cleared by ``seal``).
    """

    _forward: dict[str, str] = field(default_factory=dict)   # token -> original (reinject)
    _counters: dict[str, int] = field(default_factory=dict)  # pii_type -> count
    _dedup: dict[str, str] = field(default_factory=dict)     # original -> token (same-request dedup)
    reversible: bool = True
    # Set when entity detection was requested but failed. Redaction fails open,
    # so without this a throttled detector is indistinguishable from one that
    # found nothing — the caller sees a normal result and cannot tell that the
    # shapeless types went unchecked. Callers that care (the audit trail, the
    # admin preview panel) can report the degradation instead of hiding it.
    ner_error: str | None = None

    def add(self, pii_type: str, original: str) -> str:
        existing = self._dedup.get(original)
        if existing is not None:
            return existing
        count = self._counters.get(pii_type, 0) + 1
        self._counters[pii_type] = count
        token = f"[{pii_type.upper()}_{count}]"
        self._dedup[original] = token
        if self.reversible:
            self._forward[token] = original
        return token

    def reinject(self, text: str) -> str:
        if not self.reversible:
            return text
        for token, original in self._forward.items():
            text = text.replace(token, original)
        return text

    def seal(self) -> None:
        """Drop retained plaintext when redaction is permanent (no reinject)."""
        if not self.reversible:
            self._dedup.clear()

    @property
    def redacted_count(self) -> int:
        return sum(self._counters.values())


class PIIRedactor:
    """Policy-driven PII redaction engine.

    If the resolved policy has pii_redaction_enabled=False (and the env default
    is not set), all methods are no-ops with zero overhead. See
    ``effective_policy`` for how the AXON_PII_REDACTION_DEFAULT env flag makes a
    deploy safe-by-default without changing per-policy behavior.

    An optional ``entity_detector`` adds named-entity detection for the PII types
    regex cannot express — names, addresses, ages. It is used only by the
    ``*_async`` methods and only when a policy enables it, so the synchronous
    paths and every deploy that doesn't opt in are byte-for-byte unchanged and
    pay nothing. See ``pii_ner`` for why it is off by default (it costs more per
    request than the model's own input tokens).
    """

    def __init__(self, entity_detector=None) -> None:
        self._entity_detector = entity_detector

    def _ner_enabled(self, policy: ResolvedPolicy) -> bool:
        if self._entity_detector is None:
            return False
        if getattr(policy, "pii_ner_enabled", False):
            return True
        # Env default only applies when the policy hasn't spoken, mirroring
        # effective_policy for the regex layer.
        from src.gateway.security.pii_ner import env_ner_enabled
        return env_ner_enabled()

    def _ner_types(self, policy: ResolvedPolicy) -> list[str]:
        from src.gateway.security.pii_ner import env_ner_types
        return list(getattr(policy, "pii_ner_types", None) or env_ner_types())

    async def _ner_spans(
        self, text: str, policy: ResolvedPolicy, mapping: RedactionMapping | None = None
    ) -> list[tuple[int, int, str]]:
        """NER spans for one string, or [] if detection is off or fails.

        Fail-open: a Comprehend outage degrades to regex-only redaction rather
        than failing the request. That is the same call the semantic cache makes
        on an embedding error, and the tradeoff is explicit — a request whose
        name goes unredacted is worse than one that errors, but a gateway that
        rejects all traffic when an optional detector is down is worse still.
        The warning is the audit signal that it happened.
        """
        if not self._ner_enabled(policy):
            return []
        active = self._ner_types(policy)
        if not active:
            return []
        try:
            return await self._entity_detector.detect(text, active)
        except Exception as exc:
            logger.warning(
                "pii ner: detection failed, falling back to regex-only: %s", exc)
            if mapping is not None:
                mapping.ner_error = str(exc)[:200]
            return []

    async def _redact_str_async(
        self, text: str, active_types: list[str], mapping: RedactionMapping,
        policy: ResolvedPolicy,
    ) -> str:
        """Redact one string using both detectors, as a single span set.

        Merged before replacement rather than run in two passes: a second pass
        over already-tokenised text would compute offsets against a string the
        first pass changed, and could redact inside a ``[NAME_1]`` token.
        """
        spans = self._regex_spans(text, active_types)
        spans.extend(await self._ner_spans(text, policy, mapping))
        return self._apply_spans(text, spans, mapping)

    async def _redact_content_async(
        self, content, active_types: list[str], mapping: RedactionMapping,
        policy: ResolvedPolicy,
    ):
        """Async twin of ``_redact_content``, handling str and list content."""
        if isinstance(content, str):
            return await self._redact_str_async(content, active_types, mapping, policy)
        if isinstance(content, list):
            new_parts = []
            for part in content:
                if isinstance(part, dict):
                    key = "text" if isinstance(part.get("text"), str) else (
                        "content" if isinstance(part.get("content"), str) else None)
                    if key is not None:
                        new_parts.append({**part, key: await self._redact_str_async(
                            part[key], active_types, mapping, policy)})
                        continue
                new_parts.append(part)
            return new_parts
        return content

    async def redact_messages_async(
        self, messages: list[dict], policy: ResolvedPolicy
    ) -> tuple[list[dict], RedactionMapping]:
        """Redact across all messages, using NER when the policy enables it.

        Falls through to the synchronous implementation when NER is off, so the
        common path keeps its exact previous behaviour and cost rather than
        acquiring an await-shaped detour around nothing.
        """
        policy = self.effective_policy(policy)
        if not self._ner_enabled(policy):
            return self.redact_messages(messages, policy)

        mapping = RedactionMapping(reversible=policy.pii_reinject)
        if not policy.pii_redaction_enabled:
            return messages, mapping
        active_types = self._active_types(policy)

        redacted = []
        for msg in messages:
            if "content" not in msg:
                redacted.append(msg)
                continue
            redacted.append({**msg, "content": await self._redact_content_async(
                msg["content"], active_types, mapping, policy)})
        mapping.seal()
        return redacted, mapping

    def effective_policy(self, policy: ResolvedPolicy) -> ResolvedPolicy:
        """Apply the env default when a policy doesn't explicitly enable redaction.

        Explicit policy always wins (an org that turned redaction on/off keeps
        its choice, including its ``pii_reinject`` setting). Only when a policy
        leaves redaction off AND the env default is set do we turn it on with the
        env-configured type set. Returns the policy unchanged otherwise, so the
        Ostiari embed (which doesn't set the env flag) is never double-redacted.
        """
        if policy.pii_redaction_enabled or not env_default_enabled():
            return policy
        import dataclasses
        # replace() rather than re-listing every field: the hand-rolled rebuild
        # this used to be would silently drop any field added later, which is
        # how the NER settings went missing the first time and exactly how
        # `tools` was lost on the request path. Only the two fields this method
        # exists to change are named.
        return dataclasses.replace(
            policy,
            pii_redaction_enabled=True,
            pii_redact_types=policy.pii_redact_types or env_default_types(),
        )

    def _regex_spans(
        self, text: str, active_types: list[str]
    ) -> list[tuple[int, int, str]]:
        """Find all regex matches as (start, end, pii_type) spans."""
        spans: list[tuple[int, int, str]] = []
        for pii_type in active_types:
            pattern = PII_PATTERNS.get(pii_type)
            if pattern is None:
                continue
            for match in pattern.finditer(text):
                spans.append((match.start(), match.end(), pii_type))
        return spans

    @staticmethod
    def _drop_overlaps(
        spans: list[tuple[int, int, str]]
    ) -> list[tuple[int, int, str]]:
        """Resolve overlapping spans, keeping the longest match at each position.

        Two detectors over one string will sometimes claim overlapping text —
        Comprehend reports ADDRESS for a street line whose trailing digits also
        match the phone pattern. Replacing both would corrupt the string: the
        first replacement shifts the indices the second was computed against,
        so the second lands mid-token.

        Longest-wins because the longer span is the more complete piece of PII;
        a tie goes to whichever sorts first, which is arbitrary but stable.
        Returns spans sorted right-to-left, ready for in-place replacement.
        """
        # Longest first at each start, so the winner is seen before its overlaps.
        ordered = sorted(spans, key=lambda s: (s[0], -(s[1] - s[0])))
        kept: list[tuple[int, int, str]] = []
        last_end = -1
        for start, end, pii_type in ordered:
            if start < last_end:
                continue  # overlaps a span already kept
            kept.append((start, end, pii_type))
            last_end = end
        # Right-to-left, so each replacement leaves earlier indices valid.
        return sorted(kept, key=lambda s: s[0], reverse=True)

    def _apply_spans(
        self,
        text: str,
        spans: list[tuple[int, int, str]],
        mapping: RedactionMapping,
    ) -> str:
        """Replace spans with tokens, right-to-left to keep indices stable."""
        for start, end, pii_type in self._drop_overlaps(spans):
            token = mapping.add(pii_type, text[start:end])
            text = text[:start] + token + text[end:]
        return text

    def _redact_str(
        self, text: str, active_types: list[str], mapping: RedactionMapping
    ) -> str:
        """Redact one string, replacing right-to-left to keep indices stable."""
        return self._apply_spans(text, self._regex_spans(text, active_types), mapping)

    def _redact_content(self, content, active_types: list[str], mapping: RedactionMapping):
        """Redact a message ``content`` of any shape: str, or a list of parts.

        Multimodal / tool messages carry content as a list of parts (e.g.
        ``{"type": "text", "text": "..."}`` for OpenAI or ``{"text": "..."}`` for
        Bedrock). Previously non-str content passed through UNREDACTED, leaking
        PII in the text parts. We now redact the ``text``/``content`` string
        field of each dict part and leave non-text parts (images, etc.) intact.
        """
        if isinstance(content, str):
            return self._redact_str(content, active_types, mapping)
        if isinstance(content, list):
            new_parts = []
            for part in content:
                if isinstance(part, dict):
                    key = "text" if isinstance(part.get("text"), str) else (
                        "content" if isinstance(part.get("content"), str) else None)
                    if key is not None:
                        new_parts.append(
                            {**part, key: self._redact_str(part[key], active_types, mapping)})
                        continue
                new_parts.append(part)
            return new_parts
        return content

    def _active_types(self, policy: ResolvedPolicy) -> list[str]:
        return policy.pii_redact_types or []

    def redact(self, text: str, policy: ResolvedPolicy) -> tuple[str, RedactionMapping]:
        """Redact PII from text based on policy. Returns (redacted_text, mapping)."""
        policy = self.effective_policy(policy)
        mapping = RedactionMapping(reversible=policy.pii_reinject)
        if not policy.pii_redaction_enabled:
            return text, mapping
        active_types = self._active_types(policy)
        if not active_types:
            return text, mapping
        text = self._redact_str(text, active_types, mapping)
        mapping.seal()
        return text, mapping

    def redact_messages(
        self, messages: list[dict], policy: ResolvedPolicy
    ) -> tuple[list[dict], RedactionMapping]:
        """Redact PII across all message contents. Returns (redacted_messages, mapping)."""
        policy = self.effective_policy(policy)
        mapping = RedactionMapping(reversible=policy.pii_reinject)
        if not policy.pii_redaction_enabled:
            return messages, mapping
        active_types = self._active_types(policy)
        if not active_types:
            return messages, mapping

        redacted = []
        for msg in messages:
            if "content" not in msg:
                redacted.append(msg)
                continue
            redacted.append(
                {**msg, "content": self._redact_content(msg["content"], active_types, mapping)})
        mapping.seal()
        return redacted, mapping

    def reinject_response(self, text: str, mapping: RedactionMapping) -> str:
        """Re-inject original PII values into the LLM response (no-op if permanent)."""
        return mapping.reinject(text)
