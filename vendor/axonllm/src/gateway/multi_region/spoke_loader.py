"""Load a multi-region hub/spoke topology from YAML.

Turns ``config/spokes.yaml`` into a :class:`HubConfig`. When the file is absent
or empty, callers fall back to a single-region default — so single-region
deployments need no config and multi-region is purely additive.

Example ``spokes.yaml``::

    hub_region: us-east-1
    data_residency_strict: false
    health_check_interval_seconds: 30
    spokes:
      - region: us-east-1
        role: primary
        weight: 70
        endpoint: https://bedrock-runtime.us-east-1.amazonaws.com
        health_check_url: https://gw.use1.example.com/health
        models: [claude-sonnet, claude-haiku]
        data_residency_zones: [us]
      - region: eu-west-1
        role: active
        weight: 30
        endpoint: https://bedrock-runtime.eu-west-1.amazonaws.com
        health_check_url: https://gw.euw1.example.com/health
        data_residency_zones: [eu]
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from src.gateway.multi_region.region_config import (
    HubConfig,
    SpokeConfig,
    SpokeRole,
    SpokeStatus,
    default_single_region,
    parse_topology_integer,
)

logger = logging.getLogger(__name__)


def _spoke_from(data: dict) -> SpokeConfig:
    return SpokeConfig(
        region=data["region"],
        role=SpokeRole(data.get("role", "primary")),
        weight=parse_topology_integer("weight", data.get("weight", 100)),
        status=SpokeStatus(data.get("status", "healthy")),
        endpoint=data.get("endpoint", ""),
        providers=list(data.get("providers", []) or []),
        models=list(data.get("models", []) or []),
        data_residency_zones=list(data.get("data_residency_zones", []) or []),
        health_check_url=data.get("health_check_url", ""),
        max_latency_ms=int(data.get("max_latency_ms", 5000)),
        failover_priority=int(data.get("failover_priority", 0)),
    )


def load_hub_config(
    config_path: str = "config/spokes.yaml",
    default_region: str = "us-east-1",
) -> HubConfig:
    """Load a HubConfig from YAML, falling back to single-region.

    Missing/empty file or a spokes list with no entries → ``default_single_region``
    (so single-region deploys need no file). A malformed file is logged and also
    falls back, rather than crashing startup.
    """
    path = Path(config_path)
    if not path.exists():
        return default_single_region(region=default_region)

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        spokes_raw = raw.get("spokes") or []
        if not spokes_raw:
            return default_single_region(region=default_region)
        spokes = [_spoke_from(s) for s in spokes_raw if isinstance(s, dict)]
        if not spokes:
            return default_single_region(region=default_region)
        return HubConfig(
            hub_region=raw.get("hub_region", default_region),
            spokes=spokes,
            health_check_interval_seconds=parse_topology_integer(
                "health_check_interval_seconds",
                raw.get("health_check_interval_seconds", 30),
            ),
            failover_threshold_consecutive=int(raw.get("failover_threshold_consecutive", 3)),
            failover_cooldown_seconds=int(raw.get("failover_cooldown_seconds", 60)),
            data_residency_strict=bool(raw.get("data_residency_strict", False)),
        )
    except Exception:
        logger.warning(
            "Failed to load spokes config %s — falling back to single region",
            config_path, exc_info=True,
        )
        return default_single_region(region=default_region)
