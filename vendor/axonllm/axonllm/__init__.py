"""Public AxonLLM routing API."""

from importlib.metadata import PackageNotFoundError, version

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingData,
    EmbeddingRequest,
    EmbeddingResponse,
    ModelSummary,
    StreamChunk,
    TokenUsage,
    ValidationError,
)
from src.gateway.router import AllProvidersExhaustedError, ProviderError

from .assemblies import build_ostiari_adapter, build_router
from .hosts import (
    CredentialResolver,
    IdentityContext,
    OstiariHost,
    RouterLifecycle,
    RoutingConfigurationProvider,
    TelemetrySink,
    UsageSink,
)
from .ostiari import (
    OstiariAdapterError,
    OstiariAdapterNotStartedError,
    OstiariConfigurationError,
    OstiariResult,
    OstiariRouterAdapter,
    OstiariRoutingModeUnavailableError,
    OstiariUsageRecordingError,
)
from .router import AsyncRouter, InvalidRequestError, RouterClosedError

try:
    __version__ = version("axon-llm")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

__all__ = [
    "AllProvidersExhaustedError",
    "AsyncRouter",
    "ChatCompletionRequest",
    "ChatCompletionResponse",
    "CredentialResolver",
    "EmbeddingData",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "IdentityContext",
    "InvalidRequestError",
    "ModelSummary",
    "OstiariAdapterError",
    "OstiariAdapterNotStartedError",
    "OstiariConfigurationError",
    "OstiariHost",
    "OstiariResult",
    "OstiariRouterAdapter",
    "OstiariRoutingModeUnavailableError",
    "OstiariUsageRecordingError",
    "ProviderError",
    "RouterLifecycle",
    "RouterClosedError",
    "RoutingConfigurationProvider",
    "StreamChunk",
    "TelemetrySink",
    "TokenUsage",
    "UsageSink",
    "ValidationError",
    "__version__",
    "build_ostiari_adapter",
    "build_router",
]
