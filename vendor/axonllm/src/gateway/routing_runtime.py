"""Shared provider execution runtime for every AxonLLM delivery mode."""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    ModelSummary,
    ProviderModelMapping,
    StreamChunk,
)
from src.gateway.router import (
    AllProvidersExhaustedError,
    ProviderError,
    Router,
)
from src.gateway.routing_config import RoutingConfigSnapshot
from src.gateway.routing import NoHealthyProviderError
from src.gateway.streaming import simulate_streaming


@dataclass(frozen=True)
class OpenedProviderStream:
    """A provider stream successfully opened before any client bytes were sent."""

    mapping: ProviderModelMapping
    first_chunk: StreamChunk | None
    stream: AsyncIterator[StreamChunk]
    attempts: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ProviderStreamUnavailable:
    """No provider could open native streaming; buffered fallback may still work."""

    attempts: tuple[dict[str, Any], ...]


class RoutingRuntime:
    """Own the low-latency routing and provider transport boundary.

    The mandatory control plane and ``GatewayAgent`` remain above this object.
    Embedded, standalone, and AgentCore modes all call this same runtime for
    provider selection, fallback, streaming, and transport.
    """

    def __init__(
        self,
        *,
        router: Router,
        provider_factory: Any,
        model_registry: Any,
        validator: Any,
        owns_provider_factory: bool = False,
    ) -> None:
        self.router = router
        self.provider_factory = provider_factory
        self.model_registry = model_registry
        self.validator = validator
        self._owns_provider_factory = owns_provider_factory
        self._closed = False

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
    ) -> RoutingRuntime:
        """Build the shared runtime from strictly validated local files."""
        from src.gateway.config_loader import load_pricing_config
        from src.gateway.cost_tracker import CostTracker
        from src.gateway.health_tracker import ProviderHealthTracker
        from src.gateway.model_registry import ModelRegistry
        from src.gateway.multi_provider_factory import MultiProviderFactory
        from src.gateway.provider_loader import load_provider_routes
        from src.gateway.request_validator import RequestValidator

        model_document = yaml.safe_load(
            Path(models).read_text(encoding="utf-8")
        )
        if not isinstance(model_document, dict):
            raise ValueError(
                "model configuration must contain a YAML object"
            )
        registry = ModelRegistry.from_config(model_document)
        pricing_config = (
            load_pricing_config(str(pricing)) if pricing is not None else {}
        )
        routes = load_provider_routes(str(providers))
        provider_set = (
            frozenset(enabled_providers)
            if enabled_providers is not None
            else None
        )
        factory = MultiProviderFactory(
            bedrock_region=bedrock_region,
            enabled_providers=provider_set,
            provider_routes=routes,
        )
        try:
            router = Router(
                registry,
                ProviderHealthTracker(),
                max_retries=max_retries,
                base_delay=base_delay,
                cooldown_seconds=cooldown_seconds,
                cost_tracker=CostTracker(pricing_config),
                available_providers=factory.available_providers,
                require_priced_mappings=require_priced_mappings,
            )
            validator = RequestValidator(registry)
        except BaseException:
            factory.close_credential_providers()
            raise
        return cls(
            router=router,
            provider_factory=factory,
            model_registry=registry,
            validator=validator,
            owns_provider_factory=True,
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def available_providers(self) -> frozenset[str]:
        return self.provider_factory.available_providers

    def route_snapshot(self) -> list[dict]:
        """Return the provider factory's credential-free route view."""
        return self.provider_factory.route_snapshot()

    def config_snapshot(self) -> RoutingConfigSnapshot:
        """Return the active control-plane routing configuration."""
        return RoutingConfigSnapshot.from_registry(self.model_registry)

    def list_models(self) -> list[ModelSummary]:
        """List logical models with at least one route available here."""
        summaries: list[ModelSummary] = []
        for model in self.model_registry.list_models():
            mappings = self.router.available_mappings(model.name)
            if not mappings:
                continue
            summaries.append(
                ModelSummary(
                    name=model.name,
                    description=model.description,
                    providers=sorted(
                        {mapping.provider for mapping in mappings}
                    ),
                    capabilities=list(model.capabilities or []),
                    routing_strategy=model.routing_strategy.value,
                )
            )
        return summaries

    def provider_fn(
        self,
        request: ChatCompletionRequest,
        *,
        prompt_caching_enabled: bool = False,
        spoke: Any = None,
    ) -> Any:
        if not prompt_caching_enabled and spoke is None:
            return self.provider_factory.create(request)
        return self.provider_factory.create(
            request,
            prompt_caching_enabled=prompt_caching_enabled,
            spoke=spoke,
        )

    async def complete(
        self,
        request: ChatCompletionRequest,
        *,
        preferred_provider: str | None = None,
        allowed_models: Collection[str] | None = None,
        prompt_caching_enabled: bool = False,
        spoke: Any = None,
        provider_fn: Any = None,
    ) -> ChatCompletionResponse:
        """Execute one completion through AxonLLM's retry/fallback engine."""
        if self._closed:
            raise RuntimeError("AxonLLM routing runtime is closed")
        callback = provider_fn or self.provider_fn(
            request,
            prompt_caching_enabled=prompt_caching_enabled,
            spoke=spoke,
        )
        return await self.router.execute_with_fallback(
            request,
            callback,
            preferred_provider=preferred_provider,
            allowed_models=(
                set(allowed_models)
                if allowed_models is not None
                else None
            ),
        )

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        preferred_provider: str | None = None,
        allowed_models: Collection[str] | None = None,
    ) -> EmbeddingResponse:
        """Execute one embeddings request through retry and fallback."""
        if self._closed:
            raise RuntimeError("AxonLLM routing runtime is closed")
        provider_fn = self.provider_factory.create_embeddings(request)
        return await self.router.execute_with_fallback(
            request,  # type: ignore[arg-type]
            provider_fn,  # type: ignore[arg-type]
            preferred_provider=preferred_provider,
            allowed_models=(
                set(allowed_models)
                if allowed_models is not None
                else None
            ),
        )  # type: ignore[return-value]

    def stream_chain(
        self,
        model: str,
        preferred_provider: str | None = None,
    ) -> list[ProviderModelMapping]:
        """Return the same initial strategy and fallback order used normally."""
        mappings = self.router.available_mappings(model)
        if preferred_provider:
            return sorted(
                mappings,
                key=lambda mapping: (
                    mapping.provider != preferred_provider,
                    mapping.fallback_order,
                ),
            )
        try:
            initial = self.router._get_strategy(model).select(
                mappings,
                self.router.health_tracker,
            )
        except NoHealthyProviderError:
            return sorted(mappings, key=lambda mapping: mapping.fallback_order)
        return [
            initial,
            *sorted(
                [
                    mapping
                    for mapping in mappings
                    if mapping is not initial
                ],
                key=lambda mapping: mapping.fallback_order,
            ),
        ]

    async def open_stream(
        self,
        request: ChatCompletionRequest,
        *,
        preferred_provider: str | None = None,
        allowed_models: Collection[str] | None = None,
        prompt_caching_enabled: bool = False,
        spoke: Any = None,
    ) -> OpenedProviderStream | ProviderStreamUnavailable:
        """Open native provider streaming with pre-first-byte fallback."""
        if self._closed:
            raise RuntimeError("AxonLLM routing runtime is closed")
        if allowed_models is not None and request.model not in allowed_models:
            return ProviderStreamUnavailable(
                (
                    {
                        "provider": "none",
                        "status_code": 403,
                        "message": (
                            f"Model '{request.model}' is not in the "
                            "allowed models list"
                        ),
                    },
                )
            )

        attempts: list[dict[str, Any]] = []
        for mapping in self.stream_chain(
            request.model,
            preferred_provider,
        ):
            if not self.router.health_tracker.is_healthy(mapping.provider):
                attempts.append(
                    {
                        "provider": mapping.provider,
                        "status_code": 0,
                        "message": "skipped (unhealthy)",
                    }
                )
                continue

            stream = self.provider_factory.execute_streaming(
                request,
                mapping,
                prompt_caching_enabled=prompt_caching_enabled,
                spoke=spoke,
            )
            try:
                first = await stream.__anext__()
            except StopAsyncIteration:
                return OpenedProviderStream(
                    mapping=mapping,
                    first_chunk=None,
                    stream=stream,
                    attempts=tuple(attempts),
                )
            except ProviderError as exc:
                attempts.append(
                    {
                        "provider": mapping.provider,
                        "status_code": exc.status_code,
                        "message": exc.message,
                        **(
                            {"route_id": exc.route_id}
                            if exc.route_id
                            else {}
                        ),
                    }
                )
                if exc.provider_unavailable is not False:
                    self.router.health_tracker.mark_unhealthy(
                        mapping.provider,
                        self.router.cooldown_seconds,
                    )
                close = getattr(stream, "aclose", None)
                if callable(close):
                    await close()
                continue
            return OpenedProviderStream(
                mapping=mapping,
                first_chunk=first,
                stream=stream,
                attempts=tuple(attempts),
            )

        return ProviderStreamUnavailable(tuple(attempts))

    async def stream(
        self,
        request: ChatCompletionRequest,
        *,
        preferred_provider: str | None = None,
        allowed_models: Collection[str] | None = None,
        prompt_caching_enabled: bool = False,
        spoke: Any = None,
    ) -> AsyncIterator[StreamChunk]:
        """Yield a native stream or a buffered stream through the same router."""
        opened = await self.open_stream(
            request,
            preferred_provider=preferred_provider,
            allowed_models=allowed_models,
            prompt_caching_enabled=prompt_caching_enabled,
            spoke=spoke,
        )
        if isinstance(opened, OpenedProviderStream):
            if opened.first_chunk is not None:
                opened.first_chunk.model = request.model
                yield opened.first_chunk
            try:
                async for chunk in opened.stream:
                    chunk.model = request.model
                    yield chunk
            finally:
                close = getattr(opened.stream, "aclose", None)
                if callable(close):
                    await close()
            return

        buffered_request = replace(request, stream=False)
        try:
            response = await self.complete(
                buffered_request,
                preferred_provider=preferred_provider,
                allowed_models=allowed_models,
                prompt_caching_enabled=prompt_caching_enabled,
                spoke=spoke,
            )
        except AllProvidersExhaustedError as exc:
            raise AllProvidersExhaustedError(
                [*opened.attempts, *exc.attempts]
            ) from exc
        for chunk in simulate_streaming(response):
            yield chunk

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_provider_factory:
            await self.provider_factory.close()
