"""Abstract base class for provider adapters."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    HealthStatus,
    ModelInfo,
    ProviderHealth,
    StreamChunk,
)


class ProviderAdapter(ABC):
    """Interface that all provider adapters must implement.

    Subclasses that set PROVIDER_NAME and _MODELS get default
    implementations of list_models() and health_check().
    """

    PROVIDER_NAME: str = ""
    _MODELS: list[ModelInfo] = []
    supports_embeddings: bool = False

    def validate_request(self, request: ChatCompletionRequest) -> None:
        """Reject provider-specific capability mismatches before transport."""

    @abstractmethod
    async def translate_request(
        self, request: ChatCompletionRequest, *, prompt_caching_enabled: bool = False
    ) -> dict:
        """Translate unified request to provider-native format.

        Unsupported parameters are ignored and a warning is added.
        """
        ...

    @abstractmethod
    def translate_response(self, provider_response: dict) -> ChatCompletionResponse:
        """Translate provider response to unified format."""
        ...

    @abstractmethod
    def translate_stream_chunk(self, chunk: dict) -> StreamChunk:
        """Translate a single streaming chunk to unified SSE format."""
        ...

    def stream_translator(self) -> Callable[[dict], StreamChunk]:
        """Return request-local stream translation state."""
        return self.translate_stream_chunk

    async def translate_embedding_request(
        self,
        request: EmbeddingRequest,
    ) -> dict:
        """Translate an embeddings request when the provider supports it."""
        raise NotImplementedError(
            f"{self.PROVIDER_NAME or 'provider'} does not support embeddings"
        )

    def translate_embedding_response(
        self,
        provider_response: dict,
    ) -> EmbeddingResponse:
        """Translate an embeddings response when the provider supports it."""
        raise NotImplementedError(
            f"{self.PROVIDER_NAME or 'provider'} does not support embeddings"
        )

    async def list_models(self) -> list[ModelInfo]:
        """Return available models from this provider."""
        return list(self._MODELS)

    async def health_check(self) -> ProviderHealth:
        """Check provider connectivity and return health status."""
        return ProviderHealth(
            provider=self.PROVIDER_NAME,
            status=HealthStatus.HEALTHY,
            last_check=datetime.now(timezone.utc),
        )
