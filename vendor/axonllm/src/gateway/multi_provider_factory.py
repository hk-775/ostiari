"""Build provider callables from configured AWS SDK and HTTP transport routes."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING

from src.gateway.adapters.registry import AdapterRegistry
from src.gateway.http_client import HttpClient
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    ProviderModelMapping,
)
from src.gateway.provider_config import ProviderConfig
from src.gateway.provider_routes import (
    NoAvailableRouteError,
    ProviderRoute,
    ProviderRoutePool,
    RouteLease,
)
from src.gateway.router import ProviderError

if TYPE_CHECKING:
    from src.gateway.multi_region.region_config import SpokeConfig


logger = logging.getLogger(__name__)

_ADAPTER_TYPES = {
    "ai21": ("src.gateway.adapters.ai21_adapter", "AI21Adapter"),
    "anthropic": (
        "src.gateway.adapters.anthropic_adapter",
        "AnthropicAdapter",
    ),
    "azure_openai": (
        "src.gateway.adapters.azure_adapter",
        "AzureOpenAIAdapter",
    ),
    "bedrock": (
        "src.gateway.adapters.bedrock_adapter",
        "BedrockAdapter",
    ),
    "bedrock-mantle": (
        "src.gateway.adapters.mantle_adapter",
        "MantleAdapter",
    ),
    "cohere": ("src.gateway.adapters.cohere_adapter", "CohereAdapter"),
    "fireworks": (
        "src.gateway.adapters.fireworks_adapter",
        "FireworksAdapter",
    ),
    "google_ai": (
        "src.gateway.adapters.google_ai_adapter",
        "GoogleAIAdapter",
    ),
    "groq": ("src.gateway.adapters.groq_adapter", "GroqAdapter"),
    "openai": ("src.gateway.adapters.openai_adapter", "OpenAIAdapter"),
    "together": (
        "src.gateway.adapters.together_adapter",
        "TogetherAdapter",
    ),
    "vertex_ai": (
        "src.gateway.adapters.vertex_adapter",
        "VertexAIAdapter",
    ),
    "xai": ("src.gateway.adapters.xai_adapter", "XAIAdapter"),
}


def _load_adapter(module_name: str, class_name: str):
    module = importlib.import_module(module_name)
    adapter_type = getattr(module, class_name)
    return adapter_type()


def create_bedrock_provider_fn(*args, **kwargs):
    """Import the AWS transport only when a Bedrock route is first used."""
    try:
        from src.gateway.bedrock_provider import (
            create_bedrock_provider_fn as implementation,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Bedrock routes require the 'axon-llm[bedrock]' extra"
        ) from exc
    return implementation(*args, **kwargs)


def create_mantle_provider_fn(*args, **kwargs):
    """Import the SigV4 Mantle transport only when its route is first used."""
    try:
        from src.gateway.mantle_provider import (
            create_mantle_provider_fn as implementation,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Bedrock Mantle routes require the 'axon-llm[bedrock]' extra"
        ) from exc
    return implementation(*args, **kwargs)


class MultiProviderFactory:
    """Creates provider_fn callables that dispatch to the right backend.

    Bedrock calls go through boto3. Everything else goes through HttpClient.
    """

    def __init__(
        self,
        provider_configs: dict[str, ProviderConfig] | None = None,
        bedrock_region: str = "us-east-1",
        enabled_providers: frozenset[str] | None = None,
        provider_routes: list[ProviderRoute] | None = None,
    ) -> None:
        self._adapter_registry = AdapterRegistry()
        for provider_name, (module_name, class_name) in _ADAPTER_TYPES.items():
            self._adapter_registry.register_lazy(
                provider_name,
                lambda module_name=module_name, class_name=class_name: (
                    _load_adapter(module_name, class_name)
                ),
            )

        self._provider_configs = provider_configs or {}
        self._enabled_providers = enabled_providers
        self._http_client = HttpClient()
        self._bedrock_region = bedrock_region
        self._credential_providers = {
            config.provider_name: config.credential_provider
            for config in self._provider_configs.values()
            if config.credential_provider is not None
        }
        self._owned_credential_providers: dict[int, object] = {
            id(provider): provider
            for provider in self._credential_providers.values()
        }
        try:
            routes = list(provider_routes or [])
            if not routes:
                routes.extend(
                    ProviderRoute.from_provider_config(config)
                    for config in self._provider_configs.values()
                )
            for route in routes:
                if route.credential_provider is not None:
                    self._credential_providers[route.provider] = (
                        route.credential_provider
                    )
                    self._own_credential_provider(
                        route.credential_provider
                    )
            routes = self._resolve_credential_providers(routes)
            configured = {route.provider for route in routes}
            if "bedrock" not in configured:
                routes.append(
                    ProviderRoute(
                        route_id="bedrock:default",
                        provider="bedrock",
                        auth_type="aws_credentials",
                        credentials={"region": bedrock_region},
                        region=bedrock_region,
                    )
                )
            if "bedrock-mantle" not in configured:
                routes.append(
                    ProviderRoute(
                        route_id="bedrock-mantle:default",
                        provider="bedrock-mantle",
                        auth_type="aws_credentials",
                        credentials={"region": bedrock_region},
                        region=bedrock_region,
                    )
                )
            self._route_pool = ProviderRoutePool(routes)
            self._bedrock_by_route: dict[str, Callable] = {}
            self._mantle_by_route: dict[str, Callable] = {}
            # Region-keyed views remain for compatibility; route-keyed caches
            # are authoritative when multiple credentials share a region.
            self._bedrock_by_region: dict[str, Callable] = {}
            self._mantle_by_region: dict[str, Callable] = {}
            self._refresh_legacy_configs()
        except BaseException:
            self.close_credential_providers()
            raise

    @property
    def available_providers(self) -> frozenset[str]:
        """Providers that this process has credentials and permission to invoke."""
        available = set(self._route_pool.providers)
        if self._enabled_providers is not None:
            available.intersection_update(self._enabled_providers)
        return frozenset(available)

    @property
    def route_pool(self) -> ProviderRoutePool:
        return self._route_pool

    def configure_routes(
        self, routes: list[dict] | list[ProviderRoute]
    ) -> dict[str, int]:
        """Atomically replace the route catalog used for future attempts."""
        parsed = [
            route if isinstance(route, ProviderRoute) else ProviderRoute.from_dict(route)
            for route in routes
        ]
        parsed = self._resolve_credential_providers(parsed)
        self._route_pool.replace(parsed)
        self._bedrock_by_route.clear()
        self._mantle_by_route.clear()
        self._bedrock_by_region.clear()
        self._mantle_by_region.clear()
        self._refresh_legacy_configs()
        self._http_client.retain_configs(
            [
                route.to_provider_config()
                for route in parsed
                if route.provider not in {"bedrock", "bedrock-mantle"}
            ]
        )
        return {
            "routes": len(parsed),
            "providers": len(self.available_providers),
        }

    def _resolve_credential_providers(
        self,
        routes: list[ProviderRoute],
    ) -> list[ProviderRoute]:
        """Bind runtime-only refreshable credentials to route documents."""
        resolved: list[ProviderRoute] = []
        for route in routes:
            credential_provider = route.credential_provider
            if credential_provider is not None:
                self._credential_providers[route.provider] = (
                    credential_provider
                )
                self._own_credential_provider(credential_provider)
            elif route.auth_type == "gcp_service_account":
                credential_provider = self._credential_providers.get(
                    route.provider
                )
                if credential_provider is None:
                    raise ValueError(
                        "Vertex AI routes require refreshable Google "
                        "credentials configured on the gateway"
                    )
                route = dataclasses.replace(
                    route,
                    credentials={"credential_source": "google-auth"},
                    credential_provider=credential_provider,
                )
            resolved.append(route)
        return resolved

    def _own_credential_provider(self, provider: object) -> None:
        self._owned_credential_providers[id(provider)] = provider

    def close_credential_providers(self) -> None:
        """Synchronously stop all unique refreshers owned by this factory."""
        providers = tuple(self._owned_credential_providers.values())
        self._owned_credential_providers.clear()
        for provider in providers:
            close = getattr(provider, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception:
                logger.warning(
                    "provider credential refresher shutdown failed",
                    exc_info=True,
                )

    def route_snapshot(self) -> list[dict]:
        return self._route_pool.snapshot()

    def _refresh_legacy_configs(self) -> None:
        """Keep compatibility readers pointed at the first route per provider."""
        self._provider_configs = {}
        for route in sorted(
            self._route_pool.routes(),
            key=lambda item: (item.provider, item.priority, item.route_id),
        ):
            if route.provider not in {"bedrock", "bedrock-mantle"}:
                self._provider_configs.setdefault(
                    route.provider, route.to_provider_config()
                )

    def config_for(
        self,
        provider: str,
        spoke: SpokeConfig | None = None,
        model_id: str = "",
    ) -> ProviderConfig | None:
        """Peek at an eligible concrete config without reserving route capacity."""
        route = self._route_pool.peek(provider, model_id)
        if route is None or route.provider in {"bedrock", "bedrock-mantle"}:
            return None
        return self._config_for_route(route, spoke)

    def _config_for_route(
        self,
        route: ProviderRoute,
        spoke: SpokeConfig | None,
    ) -> ProviderConfig:
        base = route.to_provider_config()
        if spoke is None:
            return base
        overrides: dict = {}
        if spoke.endpoint:
            overrides["base_url"] = spoke.endpoint
        if spoke.region and base.auth_type == "aws_credentials":
            creds = dict(base.credentials)
            creds["region"] = spoke.region
            overrides["credentials"] = creds
        if not overrides:
            return base
        return dataclasses.replace(base, **overrides)

    def _bedrock_fn_for(self, route: ProviderRoute, region: str) -> Callable:
        key = f"{route.fingerprint()}:{region}"
        fn = self._bedrock_by_route.get(key)
        if fn is None:
            fn = create_bedrock_provider_fn(
                region=region,
                endpoint_url=route.endpoint,
                credentials=route.credentials,
                connect_timeout=route.connect_timeout,
                read_timeout=route.read_timeout,
            )
            self._bedrock_by_route[key] = fn
            self._bedrock_by_region.setdefault(region, fn)
        return fn

    def _mantle_fn_for(self, route: ProviderRoute, region: str) -> Callable:
        key = f"{route.fingerprint()}:{region}"
        fn = self._mantle_by_route.get(key)
        if fn is None:
            fn = create_mantle_provider_fn(
                region=region,
                endpoint_url=route.endpoint,
                credentials_config=route.credentials,
                connect_timeout=route.connect_timeout,
                read_timeout=route.read_timeout,
            )
            self._mantle_by_route[key] = fn
            self._mantle_by_region.setdefault(region, fn)
        return fn

    def _route_allowed(self, provider: str) -> bool:
        return (
            self._enabled_providers is None
            or provider in self._enabled_providers
        )

    def _no_route_error(
        self,
        mapping: ProviderModelMapping,
        exc: NoAvailableRouteError,
    ) -> ProviderError:
        return ProviderError(
            status_code=503,
            provider=mapping.provider,
            message=(
                f"No eligible route for provider '{mapping.provider}' "
                f"and model '{mapping.model_id}'"
            ),
            retryable=exc.temporarily_unavailable,
            provider_unavailable=not exc.temporarily_unavailable,
        )

    def _routed_error(
        self,
        mapping: ProviderModelMapping,
        lease: RouteLease,
        exc: ProviderError,
    ) -> ProviderError:
        route = lease.route
        alternate = self._route_pool.has_available(
            mapping.provider,
            mapping.model_id,
            exclude_route_id=route.route_id,
            exclude_generation=lease.generation,
        )
        route_scoped = (
            exc.status_code in {401, 402, 403, 404, 429}
            or exc.status_code >= 500
        )
        return ProviderError(
            status_code=exc.status_code,
            provider=mapping.provider,
            message=exc.message,
            route_id=route.route_id,
            retryable=True if alternate and route_scoped else exc.retryable,
            provider_unavailable=not self._route_pool.has_available(
                mapping.provider, mapping.model_id
            ),
        )

    async def _execute_route(
        self,
        request: ChatCompletionRequest,
        mapping: ProviderModelMapping,
        *,
        prompt_caching_enabled: bool,
        spoke: SpokeConfig | None,
    ) -> ChatCompletionResponse:
        try:
            lease = self._route_pool.acquire(mapping.provider, mapping.model_id)
        except NoAvailableRouteError as exc:
            raise self._no_route_error(mapping, exc) from exc

        route = lease.route
        region = (
            spoke.region
            if spoke and spoke.region
            else route.region or self._bedrock_region
        )
        started = time.monotonic()
        settled = False
        try:
            if mapping.provider == "bedrock-mantle":
                response = await self._mantle_fn_for(route, region)(
                    request,
                    prompt_caching_enabled=prompt_caching_enabled,
                )(mapping)
            elif mapping.provider == "bedrock":
                response = await self._bedrock_fn_for(route, region)(
                    request,
                    prompt_caching_enabled=prompt_caching_enabled,
                )(mapping)
            else:
                try:
                    adapter = self._adapter_registry.get(mapping.provider)
                except KeyError:
                    raise ProviderError(
                        status_code=500,
                        provider=mapping.provider,
                        message=(
                            "No adapter registered for provider "
                            f"'{mapping.provider}'"
                        ),
                    ) from None
                config = self._config_for_route(route, spoke)
                response = await self._http_client.execute(
                    request,
                    mapping,
                    adapter,
                    config,
                    prompt_caching_enabled=prompt_caching_enabled,
                )
        except ProviderError as exc:
            self._route_pool.record_failure(lease, exc.status_code)
            settled = True
            raise self._routed_error(mapping, lease, exc) from exc
        except Exception as exc:
            self._route_pool.record_failure(lease, 0)
            settled = True
            wrapped = ProviderError(
                502,
                mapping.provider,
                "Provider route transport failed",
            )
            raise self._routed_error(mapping, lease, wrapped) from exc
        else:
            self._route_pool.record_success(
                lease,
                latency_ms=(time.monotonic() - started) * 1000,
                output_tokens=response.usage.completion_tokens,
            )
            settled = True
            return response
        finally:
            # Cancellation is not a provider failure, but it must release
            # reserved route capacity.
            if not settled:
                self._route_pool.release(lease)

    def create(
        self,
        request: ChatCompletionRequest,
        prompt_caching_enabled: bool = False,
        spoke: SpokeConfig | None = None,
    ) -> Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]:
        """Return a provider_fn that dispatches based on provider type.

        When multi-region routing supplies a ``spoke`` in a non-default region,
        Bedrock/Mantle calls use a client bound to that region and HTTP-provider
        calls use the spoke's endpoint/region override.
        """
        if spoke and spoke.region:
            # Preserve eager spoke validation from the pre-route-pool factory.
            # Actual execution still selects and caches by concrete route.
            for route in self._route_pool.routes():
                if not route.enabled:
                    continue
                if route.provider == "bedrock":
                    self._bedrock_fn_for(route, spoke.region)
                    break
            for route in self._route_pool.routes():
                if not route.enabled:
                    continue
                if route.provider == "bedrock-mantle":
                    self._mantle_fn_for(route, spoke.region)
                    break

        async def _provider_fn(mapping: ProviderModelMapping) -> ChatCompletionResponse:
            if not self._route_allowed(mapping.provider):
                raise ProviderError(
                    status_code=503,
                    provider=mapping.provider,
                    message=(
                        f"Provider '{mapping.provider}' is disabled or "
                        "not configured in this deployment"
                    ),
                )
            return await self._execute_route(
                request,
                mapping,
                prompt_caching_enabled=prompt_caching_enabled,
                spoke=spoke,
            )

        return _provider_fn

    async def _execute_embedding_route(
        self,
        request: EmbeddingRequest,
        mapping: ProviderModelMapping,
    ) -> EmbeddingResponse:
        try:
            lease = self._route_pool.acquire(
                mapping.provider,
                mapping.model_id,
            )
        except NoAvailableRouteError as exc:
            raise self._no_route_error(mapping, exc) from exc

        started = time.monotonic()
        settled = False
        try:
            adapter = self._adapter_registry.get(mapping.provider)
            config = self._config_for_route(lease.route, None)
            response = await self._http_client.execute_embeddings(
                request,
                mapping,
                adapter,
                config,
            )
        except ProviderError as exc:
            self._route_pool.record_failure(lease, exc.status_code)
            settled = True
            raise self._routed_error(mapping, lease, exc) from exc
        except Exception as exc:
            self._route_pool.record_failure(lease, 0)
            settled = True
            wrapped = ProviderError(
                502,
                mapping.provider,
                "Provider embeddings transport failed",
            )
            raise self._routed_error(mapping, lease, wrapped) from exc
        else:
            self._route_pool.record_success(
                lease,
                latency_ms=(time.monotonic() - started) * 1000,
            )
            settled = True
            return response
        finally:
            if not settled:
                self._route_pool.release(lease)

    def create_embeddings(
        self,
        request: EmbeddingRequest,
    ) -> Callable[[ProviderModelMapping], Awaitable[EmbeddingResponse]]:
        """Return a route-aware embeddings provider function."""

        async def _provider_fn(
            mapping: ProviderModelMapping,
        ) -> EmbeddingResponse:
            if not self._route_allowed(mapping.provider):
                raise ProviderError(
                    status_code=503,
                    provider=mapping.provider,
                    message=(
                        f"Provider '{mapping.provider}' is disabled or "
                        "not configured in this deployment"
                    ),
                )
            try:
                adapter = self._adapter_registry.get(mapping.provider)
            except KeyError:
                raise ProviderError(
                    status_code=501,
                    provider=mapping.provider,
                    message=(
                        f"Embeddings are not supported for provider "
                        f"'{mapping.provider}'"
                    ),
                    retryable=False,
                    provider_unavailable=False,
                ) from None
            if not adapter.supports_embeddings:
                raise ProviderError(
                    status_code=501,
                    provider=mapping.provider,
                    message=(
                        f"Embeddings are not supported for provider "
                        f"'{mapping.provider}'"
                    ),
                    retryable=False,
                    provider_unavailable=False,
                )
            return await self._execute_embedding_route(request, mapping)

        return _provider_fn

    async def execute_streaming(
        self,
        request: ChatCompletionRequest,
        mapping: ProviderModelMapping,
        *,
        prompt_caching_enabled: bool = False,
        spoke: SpokeConfig | None = None,
    ) -> AsyncIterator:
        """Open one route-aware SSE stream, rotating routes before first byte."""
        if mapping.provider in {"bedrock", "bedrock-mantle"}:
            raise ProviderError(
                501,
                mapping.provider,
                "true streaming is not available for this provider transport",
                retryable=False,
                provider_unavailable=False,
            )

        attempts = max(1, self._route_pool.route_count(
            mapping.provider, mapping.model_id
        ))
        last_error: ProviderError | None = None
        for _ in range(attempts):
            try:
                lease = self._route_pool.acquire(
                    mapping.provider, mapping.model_id
                )
            except NoAvailableRouteError as exc:
                raise self._no_route_error(mapping, exc) from exc
            route = lease.route
            started = time.monotonic()
            yielded = False
            settled = False
            stream: AsyncIterator | None = None
            try:
                adapter = self._adapter_registry.get(mapping.provider)
                config = self._config_for_route(route, spoke)
                stream = self._http_client.execute_streaming(
                    request,
                    mapping,
                    adapter,
                    config,
                    prompt_caching_enabled=prompt_caching_enabled,
                )
                first = await stream.__anext__()
                yielded = True
                yield first
                final_usage = first.usage
                async for chunk in stream:
                    if chunk.usage is not None:
                        final_usage = chunk.usage
                    yield chunk
                self._route_pool.record_success(
                    lease,
                    latency_ms=(time.monotonic() - started) * 1000,
                    output_tokens=(
                        final_usage.completion_tokens if final_usage else 0
                    ),
                )
                settled = True
                return
            except StopAsyncIteration:
                self._route_pool.record_success(
                    lease,
                    latency_ms=(time.monotonic() - started) * 1000,
                )
                settled = True
                return
            except ProviderError as exc:
                self._route_pool.record_failure(lease, exc.status_code)
                settled = True
                routed = self._routed_error(mapping, lease, exc)
                last_error = routed
                if yielded or not routed.retryable:
                    raise routed from exc
            except Exception as exc:
                self._route_pool.record_failure(lease, 0)
                settled = True
                routed = self._routed_error(
                    mapping,
                    lease,
                    ProviderError(
                        502,
                        mapping.provider,
                        "Provider streaming transport failed",
                    ),
                )
                last_error = routed
                if yielded or not routed.retryable:
                    raise routed from exc
            finally:
                try:
                    if stream is not None:
                        close = getattr(stream, "aclose", None)
                        if callable(close):
                            await close()
                except Exception:  # noqa: BLE001 - cleanup cannot replace the result
                    logger.debug(
                        "provider HTTP stream close failed route=%s",
                        route.route_id,
                        exc_info=True,
                    )
                finally:
                    if not settled:
                        self._route_pool.release(lease)

        if last_error is not None:
            raise last_error
        raise ProviderError(
            503,
            mapping.provider,
            "No provider route could open a stream",
            provider_unavailable=True,
        )

    async def close(self) -> None:
        try:
            await self._http_client.close()
        finally:
            await asyncio.to_thread(self.close_credential_providers)
