"""Region and spoke configuration for hub-and-spoke topology.

Supports single-region, active-passive failover, and active-active multi-region
with the same data model — just change the spoke list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


def parse_topology_integer(name: str, value: object) -> int:
    """Normalize integer input without silently truncating fractional values."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError(f"{name} must be an integer")


def _require_non_negative_integer(name: str, value: object) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise ValueError(f"{name} must be a non-negative integer")


def _require_positive_integer(name: str, value: object) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive integer")


class SpokeRole(Enum):
    PRIMARY = "primary"
    FAILOVER = "failover"
    ACTIVE = "active"


class SpokeStatus(Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    DRAINING = "draining"


@dataclass
class SpokeConfig:
    """Configuration for a single spoke (regional deployment)."""

    region: str
    role: SpokeRole = SpokeRole.PRIMARY
    weight: int = 100
    status: SpokeStatus = SpokeStatus.HEALTHY
    endpoint: str = ""
    providers: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    data_residency_zones: list[str] = field(default_factory=list)
    health_check_url: str = ""
    max_latency_ms: int = 5000
    failover_priority: int = 0

    def __setattr__(self, name: str, value: object) -> None:
        if name == "weight":
            _require_non_negative_integer(name, value)
        super().__setattr__(name, value)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate routing fields after external deserialization."""
        _require_non_negative_integer("weight", self.weight)


@dataclass
class HubConfig:
    """Hub configuration — the control plane that routes to spokes."""

    hub_region: str
    spokes: list[SpokeConfig] = field(default_factory=list)
    health_check_interval_seconds: int = 30
    failover_threshold_consecutive: int = 3
    failover_cooldown_seconds: int = 60
    data_residency_strict: bool = False
    revision: int = 0

    def __setattr__(self, name: str, value: object) -> None:
        if name == "health_check_interval_seconds":
            _require_positive_integer(name, value)
        super().__setattr__(name, value)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate health scheduling and all routable spoke inputs."""
        _require_positive_integer(
            "health_check_interval_seconds",
            self.health_check_interval_seconds,
        )
        for spoke in self.spokes:
            spoke.validate()

    @property
    def active_spokes(self) -> list[SpokeConfig]:
        return [s for s in self.spokes if s.status == SpokeStatus.HEALTHY]

    @property
    def is_single_region(self) -> bool:
        return len(self.spokes) <= 1

    def get_spoke(self, region: str) -> SpokeConfig | None:
        return next((s for s in self.spokes if s.region == region), None)

    def get_primary(self) -> SpokeConfig | None:
        return next((s for s in self.spokes if s.role == SpokeRole.PRIMARY), None)

    def get_failover_candidates(self) -> list[SpokeConfig]:
        """Return spokes ordered by failover priority (lower = higher priority)."""
        candidates = [
            s for s in self.spokes
            if s.role in (SpokeRole.FAILOVER, SpokeRole.ACTIVE)
            and s.status == SpokeStatus.HEALTHY
        ]
        return sorted(candidates, key=lambda s: s.failover_priority)


def apply_persisted_topology(
    hub_config: HubConfig,
    loaded: dict,
    *,
    preserve_health: bool = False,
) -> None:
    """Replace a live config with one authoritative persisted snapshot.

    Parse the complete snapshot before touching the shared object. A malformed
    spoke must not leave hub settings from the new document paired with spokes
    from the old one.
    """
    previous_status = (
        {spoke.region: spoke.status for spoke in hub_config.spokes}
        if preserve_health
        else {}
    )
    revision = loaded.get("revision", 0)
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        raise ValueError("topology revision must be a non-negative integer")

    spokes: list[SpokeConfig] = []
    for stored in loaded.get("spokes", []):
        try:
            role = SpokeRole(stored.get("role", "active"))
        except ValueError as exc:
            raise ValueError(
                f"persisted spoke {stored.get('region')!r} has an unknown role"
            ) from exc
        region = stored["region"]
        spokes.append(
            SpokeConfig(
                region=region,
                role=role,
                weight=stored.get("weight", 50),
                status=previous_status.get(region, SpokeStatus.HEALTHY),
                endpoint=stored.get("endpoint", ""),
                providers=stored.get("providers", []),
                models=stored.get("models", []),
                data_residency_zones=stored.get(
                    "data_residency_zones",
                    [],
                ),
                health_check_url=stored.get("health_check_url", ""),
                max_latency_ms=stored.get("max_latency_ms", 5000),
                failover_priority=stored.get("failover_priority", 0),
            )
        )

    candidate = HubConfig(
        hub_region=loaded["hub_region"] or hub_config.hub_region,
        spokes=spokes,
        health_check_interval_seconds=loaded[
            "health_check_interval_seconds"
        ],
        failover_threshold_consecutive=loaded[
            "failover_threshold_consecutive"
        ],
        failover_cooldown_seconds=loaded[
            "failover_cooldown_seconds"
        ],
        data_residency_strict=loaded["data_residency_strict"],
        revision=revision,
    )

    # No operation below this point can fail: publish the fully parsed candidate
    # synchronously so event-loop readers cannot observe a hybrid topology.
    hub_config.hub_region = candidate.hub_region
    hub_config.spokes[:] = candidate.spokes
    hub_config.health_check_interval_seconds = (
        candidate.health_check_interval_seconds
    )
    hub_config.failover_threshold_consecutive = (
        candidate.failover_threshold_consecutive
    )
    hub_config.failover_cooldown_seconds = (
        candidate.failover_cooldown_seconds
    )
    hub_config.data_residency_strict = candidate.data_residency_strict
    hub_config.revision = candidate.revision


def default_single_region(region: str = "us-east-1") -> HubConfig:
    """Sensible default: single-region deployment."""
    return HubConfig(
        hub_region=region,
        spokes=[
            SpokeConfig(
                region=region,
                role=SpokeRole.PRIMARY,
                weight=100,
            )
        ],
    )


def active_passive(primary: str, failover: str) -> HubConfig:
    """Active-passive failover between two regions."""
    return HubConfig(
        hub_region=primary,
        spokes=[
            SpokeConfig(region=primary, role=SpokeRole.PRIMARY, weight=100),
            SpokeConfig(region=failover, role=SpokeRole.FAILOVER, weight=0, failover_priority=1),
        ],
    )


def active_active(regions: list[tuple[str, int]]) -> HubConfig:
    """Active-active across N regions with weights."""
    spokes = []
    for i, (region, weight) in enumerate(regions):
        role = SpokeRole.PRIMARY if i == 0 else SpokeRole.ACTIVE
        spokes.append(SpokeConfig(region=region, role=role, weight=weight))
    return HubConfig(hub_region=regions[0][0], spokes=spokes)
