"""Public construction seams for AxonLLM delivery adapters."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .hosts import OstiariHost
from .ostiari import OstiariRouterAdapter
from .router import AsyncRouter


def build_router(
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
    """Build only the routing data plane from local bootstrap files.

    This seam does not construct an HTTP server, control API, identity service,
    durable application state, or background worker. Hosts retain ownership of
    the returned router and must close it.
    """

    return AsyncRouter.from_files(
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


def build_ostiari_adapter(
    *,
    router: AsyncRouter,
    host: OstiariHost,
    trusted_signing_key_arn: str,
    owns_router: bool = True,
) -> OstiariRouterAdapter:
    """Bind an existing routing core to an Ostiari-owned host lifecycle."""

    return OstiariRouterAdapter(
        router,
        host,
        trusted_signing_key_arn=trusted_signing_key_arn,
        owns_router=owns_router,
    )


__all__ = ["build_ostiari_adapter", "build_router"]
