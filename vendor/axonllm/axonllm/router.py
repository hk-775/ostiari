"""Stable embedded API over the AxonLLM routing data plane."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, overload

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    ModelSummary,
    StreamChunk,
    ValidationError,
)
from src.gateway.routing_config import RoutingConfigSnapshot
from src.gateway.routing_runtime import RoutingRuntime

if TYPE_CHECKING:
    from src.gateway.model_registry import ModelRegistry
    from src.gateway.multi_provider_factory import MultiProviderFactory
    from src.gateway.request_validator import RequestValidator
    from src.gateway.router import Router


class InvalidRequestError(ValueError):
    """Raised when an embedded completion request fails validation."""

    def __init__(self, errors: Sequence[ValidationError]) -> None:
        self.errors = tuple(errors)
        detail = "; ".join(f"{error.field}: {error.message}" for error in self.errors)
        super().__init__(detail or "Invalid request")


class RouterClosedError(RuntimeError):
    """Raised when a closed embedded router is used."""


class _Completions:
    def __init__(self, owner: AsyncRouter) -> None:
        self._owner = owner

    @overload
    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: Literal[False] = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        preferred_provider: str | None = None,
    ) -> ChatCompletionResponse: ...

    @overload
    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: Literal[True],
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        preferred_provider: str | None = None,
    ) -> AsyncIterator[StreamChunk]: ...

    @overload
    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        preferred_provider: str | None = None,
    ) -> ChatCompletionResponse | AsyncIterator[StreamChunk]: ...

    async def create(
        self,
        *,
        model: str,
        messages: list[dict],
        stream: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        stop: list[str] | None = None,
        system: str | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        preferred_provider: str | None = None,
    ) -> ChatCompletionResponse | AsyncIterator[StreamChunk]:
        """Create a routed chat completion.

        When ``stream`` is true, awaiting this method returns an async iterator.
        Provider fallback is allowed only before the first chunk is yielded.
        """
        request = ChatCompletionRequest(
            model=model,
            messages=messages,
            stream=stream,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            stop=stop,
            system=system,
            tools=tools,
            tool_choice=tool_choice,
        )
        return await self._owner._complete(
            request,
            preferred_provider=preferred_provider,
        )


class _Chat:
    def __init__(self, owner: AsyncRouter) -> None:
        self.completions = _Completions(owner)


class _Embeddings:
    def __init__(self, owner: AsyncRouter) -> None:
        self._owner = owner

    async def create(
        self,
        *,
        model: str,
        input: str | list[str],
        encoding_format: Literal["float", "base64"] = "float",
        dimensions: int | None = None,
        user: str | None = None,
        preferred_provider: str | None = None,
    ) -> EmbeddingResponse:
        """Create routed embeddings while preserving input order."""
        values = [input] if isinstance(input, str) else input
        request = EmbeddingRequest(
            model=model,
            input=values,
            encoding_format=encoding_format,
            dimensions=dimensions,
            user=user,
        )
        return await self._owner._embed(
            request,
            preferred_provider=preferred_provider,
        )


class _Models:
    def __init__(self, owner: AsyncRouter) -> None:
        self._owner = owner

    async def list(self) -> list[ModelSummary]:
        """List models that have at least one enabled provider mapping."""
        self._owner._ensure_open()
        summaries: list[ModelSummary] = []
        for model in self._owner._model_registry.list_models():
            mappings = self._owner._router.available_mappings(model.name)
            if not mappings:
                continue
            summaries.append(
                ModelSummary(
                    name=model.name,
                    description=model.description,
                    providers=sorted({mapping.provider for mapping in mappings}),
                    capabilities=list(model.capabilities or []),
                    routing_strategy=model.routing_strategy.value,
                )
            )
        return summaries


class AsyncRouter:
    """Embeddable asynchronous AxonLLM multi-provider router.

    ``from_files`` is the local/bootstrap constructor. Production deployments
    should provide these files from a versioned control-plane snapshot.
    Constructing this class does not initialize the AxonLLM admin, identity,
    query, or persistence services.
    """

    def __init__(
        self,
        *,
        router: Router,
        provider_factory: MultiProviderFactory,
        model_registry: ModelRegistry,
        validator: RequestValidator,
        runtime: RoutingRuntime | None = None,
    ) -> None:
        self._runtime = runtime or RoutingRuntime(
            router=router,
            provider_factory=provider_factory,
            model_registry=model_registry,
            validator=validator,
            owns_provider_factory=True,
        )
        # Compatibility aliases for integrations that need advanced inspection.
        self._router = self._runtime.router
        self._provider_factory = self._runtime.provider_factory
        self._model_registry = self._runtime.model_registry
        self._validator = self._runtime.validator
        self._closed = False
        self.chat = _Chat(self)
        self.embeddings = _Embeddings(self)
        self.models = _Models(self)

    @classmethod
    def from_files(
        cls,
        *,
        models: str | Path,
        providers: str | Path,
        pricing: str | Path | None = None,
        enabled_providers: Iterable[str] | None = None,
        bedrock_region: str = "us-east-1",
        max_retries: int = 2,
        base_delay: float = 0.5,
        cooldown_seconds: int = 60,
        require_priced_mappings: bool = False,
    ) -> AsyncRouter:
        """Build a router from strictly validated local configuration files."""
        runtime = RoutingRuntime.from_files(
            models=models,
            providers=providers,
            pricing=pricing,
            enabled_providers=enabled_providers,
            bedrock_region=bedrock_region,
            max_retries=max_retries,
            base_delay=base_delay,
            cooldown_seconds=cooldown_seconds,
            require_priced_mappings=require_priced_mappings,
        )
        return cls(
            router=runtime.router,
            provider_factory=runtime.provider_factory,
            model_registry=runtime.model_registry,
            validator=runtime.validator,
            runtime=runtime,
        )

    @property
    def available_providers(self) -> frozenset[str]:
        """Providers with an enabled route in this router process."""
        self._ensure_open()
        return self._runtime.available_providers

    def route_snapshot(self) -> list[dict]:
        """Return a secret-free snapshot of this process's concrete routes."""
        self._ensure_open()
        return self._runtime.route_snapshot()

    def config_snapshot(self) -> RoutingConfigSnapshot:
        """Return the active credential-free routing configuration."""
        self._ensure_open()
        return self._runtime.config_snapshot()

    def apply_snapshot(self, snapshot: RoutingConfigSnapshot) -> None:
        """Atomically adopt one host-verified routing configuration snapshot."""
        self._ensure_open()
        if not isinstance(snapshot, RoutingConfigSnapshot):
            raise TypeError("snapshot must be a RoutingConfigSnapshot")
        snapshot.apply(self._model_registry)

    def configure_routes(self, routes: list[dict]) -> dict[str, int]:
        """Atomically replace concrete provider routes for future requests."""
        self._ensure_open()
        configure = getattr(self._provider_factory, "configure_routes", None)
        if not callable(configure):
            raise RuntimeError("embedded provider factory does not support route updates")
        result = configure(routes)
        self._router.available_providers = self._provider_factory.available_providers
        return result

    def knows_model(self, model: str) -> bool:
        """Return whether the active routing snapshot contains ``model``."""
        self._ensure_open()
        return bool(model) and model in self._model_registry.models

    def model_available(self, model: str) -> bool:
        """Return whether ``model`` has an invocable provider mapping."""
        self._ensure_open()
        return self.knows_model(model) and self._router.is_model_available(model)

    def has_available_models(self) -> bool:
        """Return whether any configured logical model is currently routable."""
        self._ensure_open()
        return any(self._router.is_model_available(model) for model in self._model_registry.models)

    async def close(self) -> None:
        """Release HTTP sessions and refreshable credential providers."""
        if self._closed:
            return
        self._closed = True
        await self._runtime.close()

    async def __aenter__(self) -> AsyncRouter:
        self._ensure_open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RouterClosedError("AxonLLM router is closed")

    async def _complete(
        self,
        request: ChatCompletionRequest,
        *,
        preferred_provider: str | None,
    ) -> ChatCompletionResponse | AsyncIterator[StreamChunk]:
        self._ensure_open()
        errors = self._validator.validate(request)
        if errors:
            raise InvalidRequestError(errors)
        if request.stream:
            return self._runtime.stream(
                request,
                preferred_provider=preferred_provider,
            )
        return await self._runtime.complete(
            request,
            preferred_provider=preferred_provider,
        )

    async def _embed(
        self,
        request: EmbeddingRequest,
        *,
        preferred_provider: str | None,
    ) -> EmbeddingResponse:
        self._ensure_open()
        if not isinstance(request.model, str) or not request.model.strip():
            raise ValueError("model must be a non-empty string")
        model_config = self._model_registry.models.get(request.model)
        if model_config is None:
            raise ValueError(f"Unknown model: {request.model}")
        if "embeddings" not in set(model_config.capabilities or []):
            raise ValueError(f"Model '{request.model}' is not configured for embeddings")
        if (
            not isinstance(request.input, list)
            or not request.input
            or not all(isinstance(value, str) and value for value in request.input)
        ):
            raise ValueError("input must be a non-empty string or list of non-empty strings")
        if request.encoding_format not in {"float", "base64"}:
            raise ValueError("encoding_format must be 'float' or 'base64'")
        if request.dimensions is not None and (
            not isinstance(request.dimensions, int) or isinstance(request.dimensions, bool) or request.dimensions <= 0
        ):
            raise ValueError("dimensions must be a positive integer")
        return await self._runtime.embed(
            request,
            preferred_provider=preferred_provider,
        )
