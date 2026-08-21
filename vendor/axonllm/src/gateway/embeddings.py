"""Text embeddings for the semantic cache.

One backend today (Bedrock Titan) behind a one-method protocol, because the
cache only needs ``embed(text) -> vector`` and a protocol keeps the tests off
the network.

Titan rather than a local model: ``boto3`` is already a dependency and the
gateway already talks to Bedrock, whereas sentence-transformers pulls in torch —
a multi-hundred-megabyte addition to an image that is otherwise a slim Python
base, for a vector the gateway can fetch over a call it already makes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# 1024 dims at roughly a fifth the cost of v1's 1536, and v2 is the current
# generation. The dimension is not hardcoded anywhere — cosine_similarity
# compares lengths and returns 0.0 on a mismatch, so switching models degrades
# to cache misses rather than to wrong comparisons between incompatible vectors.
DEFAULT_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"

# Titan v2 accepts up to 8192 tokens. Truncating on characters is approximate
# but only ever errs toward sending less; the alternative is a tokenizer call
# per lookup on the cache's hot path to save an input that is already cheap.
MAX_EMBED_CHARS = 20_000


@runtime_checkable
class Embedder(Protocol):
    """Anything that can turn text into a vector."""

    async def embed(self, text: str) -> list[float]: ...


class BedrockTitanEmbedder:
    """Embeds via Bedrock ``invoke_model``.

    Raises on failure rather than returning an empty vector; SemanticCache
    catches, counts, and treats it as a miss. Distinguishing "the service is
    down" from "this text embeds to nothing" is worth keeping at this layer even
    though the immediate caller collapses both — a silent empty vector would
    otherwise be compared against real ones and score 0.0, which looks like a
    legitimate no-match.
    """

    def __init__(
        self,
        region: str = "us-east-1",
        model_id: str = DEFAULT_EMBEDDING_MODEL,
        client=None,
    ) -> None:
        self._model_id = model_id
        self._region = region
        self._client = client
        self._lock = asyncio.Lock()

    def _ensure_client(self):
        """Create the boto3 client on first use.

        Lazily, because constructing one resolves credentials: building it in
        __init__ would make a gateway with no AWS credentials fail at startup
        over an optional cache, rather than running with the cache disabled.
        """
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    async def embed(self, text: str) -> list[float]:
        payload = json.dumps({"inputText": text[:MAX_EMBED_CHARS]})

        def _call():
            client = self._ensure_client()
            resp = client.invoke_model(modelId=self._model_id, body=payload)
            body = json.loads(resp["body"].read())
            return body.get("embedding") or []

        # to_thread, matching bedrock_provider: boto3 is synchronous, and calling
        # it directly would block the event loop for the duration of the request
        # — stalling every other in-flight request on a cache lookup.
        return await asyncio.to_thread(_call)


def build_embedder(region: str = "us-east-1", model_id: str | None = None) -> Embedder | None:
    """Construct the default embedder, or None if boto3 is unavailable.

    None disables the semantic cache (``SemanticCache.enabled`` is False), which
    is the correct outcome for a deploy that cannot embed: the gateway keeps
    serving, exact-match caching keeps working, and the admin surface reports
    the cache as unavailable rather than as having a 0% hit rate.
    """
    try:
        import boto3  # noqa: F401
    except ImportError:
        logger.info("semantic cache: boto3 unavailable, embeddings disabled")
        return None
    return BedrockTitanEmbedder(region=region, model_id=model_id or DEFAULT_EMBEDDING_MODEL)
