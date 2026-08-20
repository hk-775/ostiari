"""Named-entity PII detection, layered on top of the regex patterns.

Regexes can only find PII that has a shape — an SSN is three digits, a dash, two
digits, a dash, four. A person's name has no shape, which is why
``PII_PATTERNS`` has no entry for one and why "Alice Smith" passes through
untouched while five other items in the same sentence are replaced.

This module adds a second detector for the shapeless types (names, addresses,
ages) and hands its findings back as ``(start, end, pii_type)`` spans so the
existing right-to-left replacement in ``PIIRedactor._redact_str`` consumes them
the same way it consumes regex matches. The two detectors are a union, not a
replacement: Comprehend missed ``10.0.0.7`` in "Deploy to 10.0.0.7 using the
deploy_key", which the ``ip_address`` pattern catches trivially. Structured
tokens belong to the regexes; shapeless ones belong here.

Backend choice — Comprehend rather than spaCy/Presidio — is an image-size
constraint plus an accuracy one. ``boto3`` is already a dependency, so this
backend costs the image nothing; spaCy's ``en_core_web_sm`` adds ~148MB and 1.35s
of process start, and tags ``Jenkins``, ``Django`` and ``UserService`` as
PERSON/ORG — false positives on precisely the developer prompts this gateway
serves. Comprehend returns a typed, calibrated set on the same input.

The cost is real and worth stating plainly: ~$0.0001 per 100 characters with a
3-unit minimum, which exceeds the Sonnet input-token cost for the same text.
That is why this is off unless a policy turns it on, and why ``Detector`` is a
protocol — a deploy that outgrows the per-call price can swap in a local model
without touching the redactor or the policy model.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Comprehend entity type -> the PII type name used in redaction tokens.
#
# Only the types the regexes genuinely cannot do. Comprehend also detects EMAIL,
# SSN, PHONE and card numbers, but PII_PATTERNS already covers those
# deterministically and for free — paying per call to re-find them would add
# cost and duplicate spans without adding coverage.
#
# The values deliberately reuse/extend the regex naming convention so tokens
# read the same either way: [NAME_1] alongside [EMAIL_1].
NER_TYPE_MAP: dict[str, str] = {
    "NAME": "name",
    "ADDRESS": "address",
    "AGE": "age",
}

# Default entity set when a policy enables NER without naming types.
DEFAULT_NER_TYPES: tuple[str, ...] = ("name", "address", "age")

# Below this, a detection is dropped. Deliberately low, because Comprehend's
# scores do NOT separate real PII from public figures — measured: "Robert Chen,
# our new hire" scores 0.999 and "Napoleon" scores 1.000. No threshold can tell
# them apart, so this only filters genuine model uncertainty and the
# public-figure over-redaction is an accepted, documented consequence of turning
# NAME on. Raising it would trade real-PII recall for nothing.
MIN_CONFIDENCE = 0.80

# Comprehend accepts 100KB per DetectPiiEntities call. Truncating means the tail
# of a very long prompt is checked by the regexes only; that beats raising an
# exception on the request path over an optional detector.
MAX_DETECT_BYTES = 95_000


def env_ner_enabled() -> bool:
    """True when AXON_PII_NER_DEFAULT opts a deploy into NER by default.

    Mirrors ``env_default_enabled`` for the regex layer: unset → False, so no
    existing deploy starts paying per-request detection costs or changes its
    redaction behaviour by upgrading.
    """
    return os.environ.get("AXON_PII_NER_DEFAULT", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def env_ner_types() -> list[str]:
    """Entity types used when NER is on and policy names none.

    AXON_PII_NER_TYPES (comma-separated) narrows the set. Unknown names are
    dropped with a warning rather than raising, matching ``env_default_types``.
    """
    raw = os.environ.get("AXON_PII_NER_TYPES", "").strip()
    if not raw:
        return list(DEFAULT_NER_TYPES)
    known = set(NER_TYPE_MAP.values())
    types, unknown = [], []
    for t in (p.strip() for p in raw.split(",")):
        if not t:
            continue
        (types if t in known else unknown).append(t)
    if unknown:
        logger.warning("AXON_PII_NER_TYPES has unknown types (ignored): %s", unknown)
    return types or list(DEFAULT_NER_TYPES)


@runtime_checkable
class EntityDetector(Protocol):
    """Anything that can find shapeless PII spans in text.

    Returns ``(start, end, pii_type)`` using Python string indices, so the
    caller can slice ``text[start:end]`` directly.
    """

    async def detect(
        self, text: str, active_types: list[str]
    ) -> list[tuple[int, int, str]]: ...


class ComprehendEntityDetector:
    """Detects via Comprehend ``DetectPiiEntities``.

    Raises on failure rather than returning an empty list. The caller
    (``PIIRedactor``) catches and falls back to regex-only, but that decision
    belongs to the caller: an empty list here is indistinguishable from "this
    text contains no names", and silently treating an outage as a clean result
    is how a redaction layer stops redacting without anyone noticing.
    """

    def __init__(
        self,
        region: str = "us-east-1",
        client=None,
        min_confidence: float = MIN_CONFIDENCE,
    ) -> None:
        self._region = region
        self._client = client
        self._min_confidence = min_confidence

    def _ensure_client(self):
        """Create the boto3 client on first use.

        Lazily, for the same reason as the embedder: constructing one resolves
        credentials, so building it eagerly would make a gateway with no AWS
        access fail at startup over an optional detector.
        """
        if self._client is None:
            import boto3

            self._client = boto3.client("comprehend", region_name=self._region)
        return self._client

    async def detect(
        self, text: str, active_types: list[str]
    ) -> list[tuple[int, int, str]]:
        if not text.strip() or not active_types:
            return []

        wanted = set(active_types)
        # Comprehend counts bytes, not characters. Encode/truncate/decode with
        # errors="ignore" so a multi-byte character split by the boundary is
        # dropped rather than corrupting the payload.
        payload = text
        if len(text.encode("utf-8")) > MAX_DETECT_BYTES:
            payload = text.encode("utf-8")[:MAX_DETECT_BYTES].decode(
                "utf-8", errors="ignore")
            logger.warning(
                "pii ner: text truncated to %d bytes for detection; "
                "the remainder is covered by regex patterns only", MAX_DETECT_BYTES)

        def _call():
            client = self._ensure_client()
            resp = client.detect_pii_entities(Text=payload, LanguageCode="en")
            return resp.get("Entities") or []

        # to_thread because boto3 is synchronous: calling it inline would block
        # the event loop for the full round trip (~50ms measured), stalling
        # every other in-flight request behind one detection.
        entities = await asyncio.to_thread(_call)

        spans: list[tuple[int, int, str]] = []
        for ent in entities:
            pii_type = NER_TYPE_MAP.get(ent.get("Type", ""))
            if pii_type is None or pii_type not in wanted:
                continue
            if ent.get("Score", 0.0) < self._min_confidence:
                continue
            start, end = ent.get("BeginOffset"), ent.get("EndOffset")
            # Comprehend offsets index the string it was given. Guard anyway:
            # a span past the end would slice silently and produce a token in
            # the wrong place, which is worse than skipping the detection.
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            if start < 0 or end > len(payload) or start >= end:
                continue
            spans.append((start, end, pii_type))
        return spans


def build_entity_detector(region: str = "us-east-1") -> EntityDetector | None:
    """Construct the default detector, or None if boto3 is unavailable.

    None disables NER, which is the right outcome for a deploy that cannot call
    Comprehend: the gateway keeps serving and regex redaction keeps working,
    rather than every request failing over an optional enhancement.
    """
    try:
        import boto3  # noqa: F401
    except ImportError:
        logger.info("pii ner: boto3 unavailable, entity detection disabled")
        return None
    return ComprehendEntityDetector(region=region)
