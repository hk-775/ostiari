"""Factory that creates provider_fn callables for Router.execute_with_fallback."""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from src.gateway.adapters.registry import AdapterRegistry
from src.gateway.http_client import HttpClient
from src.gateway.models import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ProviderModelMapping,
)
from src.gateway.provider_config import ProviderConfig
from src.gateway.router import ProviderError

if TYPE_CHECKING:
    from src.gateway.multi_region.region_config import SpokeConfig


class ProviderFnFactory:
    """Creates ``provider_fn`` callables that bridge the Router to the HTTP layer.

    Each call to :meth:`create` captures a :class:`ChatCompletionRequest` in a
    closure and returns an ``async (ProviderModelMapping) -> ChatCompletionResponse``
    callable compatible with :meth:`Router.execute_with_fallback`.
    """

    def __init__(
        self,
        adapter_registry: AdapterRegistry,
        provider_configs: dict[str, ProviderConfig],
        http_client: HttpClient,
    ) -> None:
        self._adapter_registry = adapter_registry
        self._provider_configs = provider_configs
        self._http_client = http_client

    def config_for(
        self, provider: str, spoke: SpokeConfig | None = None,
    ) -> ProviderConfig | None:
        """Provider config for this request, applying a region spoke override.

        When multi-region routing selects a ``spoke`` that declares its own
        ``endpoint`` (and/or AWS ``region``), return a copy of the base config
        pointed at that spoke so the actual provider call hits the chosen region
        — not just the configured default. Without a spoke override this is the
        base config unchanged.
        """
        base = self._provider_configs.get(provider)
        if base is None or spoke is None:
            return base
        overrides: dict = {}
        if spoke.endpoint:
            overrides["base_url"] = spoke.endpoint
        # For AWS SigV4 providers the region is part of the signed credential;
        # point it at the spoke's region so the call is routed + signed correctly.
        if spoke.region and base.auth_type == "aws_credentials":
            creds = dict(base.credentials)
            creds["region"] = spoke.region
            overrides["credentials"] = creds
        if not overrides:
            return base
        return dataclasses.replace(base, **overrides)

    def create(
        self,
        request: ChatCompletionRequest,
        prompt_caching_enabled: bool = False,
        spoke: SpokeConfig | None = None,
    ) -> Callable[[ProviderModelMapping], Awaitable[ChatCompletionResponse]]:
        """Return a provider_fn that closes over *request* and delegates to HttpClient.

        The returned callable:
        1. Looks up the :class:`ProviderAdapter` via the :class:`AdapterRegistry`.
        2. Retrieves the :class:`ProviderConfig` for the provider (raises
           ``ProviderError(500)`` if missing).
        3. Delegates to :meth:`HttpClient.execute` for the Router's
           retry/fallback path (always non-streaming).

        For streaming requests, the real SSE stream is consumed separately
        by ``GatewayAgent._stream_response_real`` which calls
        ``HttpClient.execute_streaming`` directly.
        """

        async def _provider_fn(mapping: ProviderModelMapping) -> ChatCompletionResponse:
            # 1. Look up adapter
            adapter = self._adapter_registry.get(mapping.provider)

            # 2. Look up provider config (with region-spoke override applied)
            config = self.config_for(mapping.provider, spoke)
            if config is None:
                raise ProviderError(
                    status_code=500,
                    provider=mapping.provider,
                    message=f"No provider configuration found for '{mapping.provider}'",
                )

            # 3. Delegate to HttpClient (always non-streaming for the Router)
            return await self._http_client.execute(
                request, mapping, adapter, config,
                prompt_caching_enabled=prompt_caching_enabled,
            )

        return _provider_fn
