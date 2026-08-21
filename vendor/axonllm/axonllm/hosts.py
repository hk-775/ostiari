"""Infrastructure-neutral contracts supplied by an embedding AxonLLM host."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.gateway.models import UsageRecord
    from src.gateway.routing_config import RoutingConfigSnapshot


@dataclass(frozen=True)
class IdentityContext:
    """Request-scoped identity and authorization context supplied by a host."""

    principal_id: str
    tenant_id: str
    project_id: str
    roles: frozenset[str] = field(default_factory=frozenset)
    scopes: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for name in ("principal_id", "tenant_id", "project_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")


@runtime_checkable
class RoutingConfigurationProvider(Protocol):
    """Load and publish validated, versioned routing snapshots."""

    async def load_snapshot(self) -> RoutingConfigSnapshot:
        """Return the current last-known-good routing configuration."""

    async def publish_snapshot(
        self,
        config: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> RoutingConfigSnapshot:
        """Validate and atomically publish the next routing configuration."""


@runtime_checkable
class CredentialResolver(Protocol):
    """Resolve one opaque provider credential reference at request time."""

    async def resolve(
        self,
        *,
        provider: str,
        reference: str,
    ) -> Mapping[str, str]:
        """Return provider transport fields without persisting them in AxonLLM."""


@runtime_checkable
class TelemetrySink(Protocol):
    """Receive provider-neutral routing telemetry from an embedded router."""

    async def emit(self, event: Mapping[str, Any]) -> None:
        """Record one best-effort telemetry event."""


@runtime_checkable
class UsageSink(Protocol):
    """Receive normalized AxonLLM usage and cost records."""

    async def record(self, usage: UsageRecord) -> None:
        """Durably record one completed request."""


@runtime_checkable
class RouterLifecycle(Protocol):
    """Explicit lifecycle owned by an embedding host."""

    async def start(self) -> None:
        """Prepare host-managed dependencies."""

    async def close(self) -> None:
        """Release host-managed dependencies."""


@runtime_checkable
class OstiariHost(
    RoutingConfigurationProvider,
    CredentialResolver,
    TelemetrySink,
    UsageSink,
    RouterLifecycle,
    Protocol,
):
    """Complete structural host contract required by the Ostiari adapter.

    ``load_snapshot`` and ``publish_snapshot`` must return snapshots whose
    signatures have already been cryptographically verified by the host. The
    adapter separately binds those snapshots to its configured signing-key ARN
    and rejects revision rollback or equivocation.
    """


__all__ = [
    "CredentialResolver",
    "IdentityContext",
    "OstiariHost",
    "RouterLifecycle",
    "RoutingConfigurationProvider",
    "TelemetrySink",
    "UsageSink",
]
