"""Admin API routes for multi-region hub-and-spoke management."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.multi_region.region_config import (
    HubConfig,
    SpokeConfig,
    SpokeRole,
    apply_persisted_topology,
    parse_topology_integer,
)
from src.gateway.persistence import PersistenceConflictError

if TYPE_CHECKING:
    from src.gateway.config_sync import ConfigSyncService
    from src.gateway.multi_region.health_monitor import SpokeHealthMonitor
    from src.gateway.multi_region.region_router import RegionRouter
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)

_TENANT_ROLES = frozenset(
    {
        "tenant_admin",
        "tenant_member",
        "tenant_auditor",
    }
)
_PLATFORM_ROLES = frozenset({"admin", "platform_admin"})


def _request_context(request: Request) -> object | None:
    state = getattr(request, "state", None)
    return getattr(state, "context", None)


def _context_roles(context: object | None) -> set[str]:
    raw_roles = getattr(context, "roles", None)
    if not isinstance(raw_roles, (list, tuple, set, frozenset)):
        return set()
    return {role for role in raw_roles if isinstance(role, str)}


def _platform_scope_error(request: Request) -> JSONResponse | None:
    """Keep tenant principals away from the shared platform topology.

    RegionRouter and SpokeHealthMonitor hold one process-wide HubConfig and the
    persistence contract stores one global topology. Treating a tenant ID as a
    selector here would therefore expose or mutate another tenant's routing
    state. Missing context remains the legacy/direct-test compatibility path.
    """
    context = _request_context(request)
    if context is None:
        return None

    roles = _context_roles(context)
    tenant_id = getattr(context, "tenant_id", None)
    if tenant_id is not None and (
        not isinstance(tenant_id, str)
        or not tenant_id.strip()
        or tenant_id != tenant_id.strip()
    ):
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "type": "invalid_tenant_scope",
                    "message": "tenant_id must be a non-empty string",
                }
            },
        )

    canonical = (
        getattr(context, "principal_id", None) is not None
        or bool(roles & (_TENANT_ROLES | {"platform_admin"}))
    )
    if canonical and tenant_id is None:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "type": "invalid_tenant_scope",
                    "message": (
                        "authenticated tenant context is missing tenant_id"
                    ),
                }
            },
        )

    if roles & _TENANT_ROLES or (
        tenant_id is not None and not roles & _PLATFORM_ROLES
    ):
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "type": "authorization_error",
                    "message": (
                        "Region topology is platform-scoped and is not "
                        "addressable from a tenant context."
                    ),
                    "code": "platform_scope_required",
                }
            },
        )
    return None


class RegionAPI:
    """Admin API for multi-region topology management.

    Topology edits are persisted so they survive a restart. Without that, adding
    or removing a spoke through this API reverted at the next deploy — quietly
    restoring a region an operator had taken out of rotation, or dropping one they
    had added, while the endpoint reported success either way.

    Only configuration is written. Spoke *health* is not: see
    ``serialize_region_topology`` for why restoring a stale status is worse than
    re-probing.
    """

    def __init__(
        self,
        router: RegionRouter,
        monitor: SpokeHealthMonitor,
        persistence: DynamoPersistence | None = None,
        config_sync: ConfigSyncService | None = None,
        topology_lock: asyncio.Lock | None = None,
    ) -> None:
        self.router = router
        self.monitor = monitor
        self._persistence = persistence
        self._config_sync = config_sync
        shared_lock = getattr(config_sync, "region_lock", None)
        self._topology_lock = topology_lock or shared_lock or asyncio.Lock()

    async def _persist_topology(self, config: HubConfig) -> bool:
        """Conditionally write a staged topology.

        One item covers hub settings and all spokes, so each edit rewrites the
        set — see the comment on ``serialize_region_topology`` for why the
        topology is stored as a unit rather than a row per spoke. Its revision is
        both the compare-and-swap token and the fleet refresh signal.
        """
        if self._persistence is None or not self._persistence.enabled:
            return True

        try:
            revision = await self._persistence.save_region_topology(
                config,
                expected_revision=config.revision,
            )
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision <= config.revision
            ):
                return False
            config.revision = revision
        except PersistenceConflictError:
            raise
        except Exception:
            logger.warning(
                "Failed to persist region topology to DynamoDB", exc_info=True
            )
            return False
        return True

    def _publish_topology(self, staged: HubConfig) -> None:
        """Publish a durable candidate without replacing the shared config."""
        live = self.router.config
        live_health = {
            spoke.region: spoke.status
            for spoke in live.spokes
        }
        for spoke in staged.spokes:
            if spoke.region in live_health:
                spoke.status = live_health[spoke.region]
        live.hub_region = staged.hub_region
        live.spokes = staged.spokes
        live.health_check_interval_seconds = (
            staged.health_check_interval_seconds
        )
        live.failover_threshold_consecutive = (
            staged.failover_threshold_consecutive
        )
        live.failover_cooldown_seconds = staged.failover_cooldown_seconds
        live.data_residency_strict = staged.data_residency_strict
        live.revision = staged.revision

    async def _commit_topology(
        self,
        staged: HubConfig,
    ) -> JSONResponse | None:
        try:
            persisted = await self._persist_topology(staged)
        except PersistenceConflictError:
            await self._adopt_latest_topology()
            return self._write_conflict()
        if not persisted:
            return self._persistence_unavailable()
        self._publish_topology(staged)
        if self._config_sync is not None:
            self._config_sync.note_local_region_revision(staged.revision)
        await self.monitor.reconcile()
        return None

    async def _adopt_latest_topology(self) -> None:
        if self._persistence is None:
            return
        loader = getattr(
            self._persistence,
            "load_region_topology_snapshot",
            None,
        )
        if loader is None:
            return
        try:
            snapshot = await loader()
            if snapshot is not None:
                revision = snapshot.get("revision")
                if (
                    not isinstance(revision, int)
                    or isinstance(revision, bool)
                    or revision < 0
                ):
                    raise ValueError(
                        "topology revision must be a non-negative integer"
                    )
                if revision <= self.router.config.revision:
                    return
                apply_persisted_topology(
                    self.router.config,
                    snapshot,
                    preserve_health=True,
                )
                if self._config_sync is not None:
                    self._config_sync.note_local_region_revision(revision)
                await self.monitor.reconcile()
        except Exception:
            logger.warning(
                "Failed to refresh region topology after a write conflict",
                exc_info=True,
            )

    @staticmethod
    def _persistence_unavailable() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "type": "service_unavailable",
                    "message": "Region topology persistence is unavailable",
                }
            },
        )

    @staticmethod
    def _write_conflict() -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "error": {
                    "type": "write_conflict",
                    "code": "topology_write_conflict",
                    "message": (
                        "Region topology changed concurrently; retry the edit"
                    ),
                }
            },
        )

    async def get_topology(self, request: Request) -> JSONResponse:
        """GET /admin/regions — full topology view."""
        if error := _platform_scope_error(request):
            return error
        config = self.router.config
        return JSONResponse(content={
            "hub_region": config.hub_region,
            "mode": self._detect_mode(config),
            "data_residency_strict": config.data_residency_strict,
            "total_spokes": len(config.spokes),
            "healthy_spokes": len(config.active_spokes),
            "spokes": [
                {
                    "region": s.region,
                    "role": s.role.value,
                    "status": s.status.value,
                    "weight": s.weight,
                    "providers": s.providers,
                    "models": s.models,
                    "data_residency_zones": s.data_residency_zones,
                    "failover_priority": s.failover_priority,
                }
                for s in config.spokes
            ],
        })

    async def get_health(self, request: Request) -> JSONResponse:
        """GET /admin/regions/health — health status of all spokes."""
        if error := _platform_scope_error(request):
            return error
        return JSONResponse(content=self.monitor.get_status_summary())

    async def check_health_now(self, request: Request) -> JSONResponse:
        """POST /admin/regions/health/check — trigger immediate health check."""
        if error := _platform_scope_error(request):
            return error
        results = await self.monitor.check_all()
        return JSONResponse(content={
            "checked": len(results),
            "results": [
                {
                    "region": r.region,
                    "healthy": r.healthy,
                    "latency_ms": round(r.latency_ms, 1),
                    "status_code": r.status_code,
                    "error": r.error,
                }
                for r in results
            ],
        })

    async def route_test(self, request: Request) -> JSONResponse:
        """POST /admin/regions/route — test routing decision for given params."""
        if error := _platform_scope_error(request):
            return error
        body = await request.json() if await request.body() else {}
        model = body.get("model")
        zone = body.get("data_residency_zone")
        preferred = body.get("preferred_region")

        decision = self.router.route(
            model=model,
            data_residency_zone=zone,
            preferred_region=preferred,
        )

        if decision is None:
            return JSONResponse(
                status_code=503,
                content={"error": "No healthy spoke available for this request"},
            )

        return JSONResponse(content={
            "target_region": decision.target_spoke.region,
            "reason": decision.reason,
            "candidates_considered": decision.candidates_considered,
            "fallback_used": decision.fallback_used,
        })

    async def mark_spoke_status(self, request: Request) -> JSONResponse:
        """PUT /admin/regions/{region}/status — manually override spoke status.

        Deliberately not persisted. The override is health state, and the next
        health check overwrites it anyway — writing it would mean a restart could
        restore an UNHEALTHY that is no longer true, holding a recovered region
        out of rotation. To take a region out durably, remove or reweight the
        spoke.
        """
        if error := _platform_scope_error(request):
            return error
        region = request.path_params["region"]
        body = await request.json()
        new_status = body.get("status", "")

        spoke = self.router.config.get_spoke(region)
        if spoke is None:
            return JSONResponse(status_code=404, content={"error": f"Region '{region}' not found"})

        if new_status == "healthy":
            self.monitor.mark_healthy(region)
        elif new_status == "unhealthy":
            self.monitor.mark_unhealthy(region)
        elif new_status == "draining":
            self.monitor.mark_draining(region)
        else:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid status. Valid: healthy, unhealthy, draining"},
            )

        return JSONResponse(content={
            "region": region,
            "status": spoke.status.value,
            "message": f"Spoke {region} marked as {new_status}",
        })

    async def trigger_failover(self, request: Request) -> JSONResponse:
        """POST /admin/regions/failover — force failover from primary."""
        if error := _platform_scope_error(request):
            return error
        primary = self.router.config.get_primary()
        if primary:
            self.monitor.mark_unhealthy(primary.region)

        decision = self.router.failover()
        if decision is None:
            return JSONResponse(
                status_code=503,
                content={"error": "No failover candidates available"},
            )

        return JSONResponse(content={
            "failover_to": decision.target_spoke.region,
            "reason": decision.reason,
            "primary_marked_unhealthy": primary.region if primary else None,
        })

    async def add_spoke(self, request: Request) -> JSONResponse:
        """POST /admin/regions/spokes — add a new spoke to the topology."""
        if error := _platform_scope_error(request):
            return error
        body = await request.json()
        region = body.get("region", "")
        if not region:
            return JSONResponse(status_code=400, content={"error": "region is required"})

        role_str = body.get("role", "active")
        try:
            role = SpokeRole(role_str)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": f"Invalid role: {role_str}. Valid: primary, failover, active"})

        async with self._topology_lock:
            if self.router.config.get_spoke(region):
                return JSONResponse(status_code=409, content={"error": f"Spoke '{region}' already exists"})
            staged = deepcopy(self.router.config)
            try:
                spoke = SpokeConfig(
                    region=region,
                    role=role,
                    weight=parse_topology_integer(
                        "weight",
                        body.get("weight", 50),
                    ),
                    endpoint=body.get("endpoint", ""),
                    providers=body.get("providers", []),
                    models=body.get("models", []),
                    data_residency_zones=body.get(
                        "data_residency_zones",
                        [],
                    ),
                    failover_priority=body.get(
                        "failover_priority",
                        len(staged.spokes),
                    ),
                )
            except (TypeError, ValueError):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "weight must be a non-negative integer"
                    },
                )
            staged.spokes.append(spoke)
            if error := await self._commit_topology(staged):
                return error
        return JSONResponse(status_code=201, content={"message": f"Spoke '{region}' added", "region": region})

    async def remove_spoke(self, request: Request) -> JSONResponse:
        """DELETE /admin/regions/spokes/{region} — remove a spoke."""
        if error := _platform_scope_error(request):
            return error
        region = request.path_params["region"]
        async with self._topology_lock:
            staged = deepcopy(self.router.config)
            spoke = staged.get_spoke(region)
            if spoke is None:
                return JSONResponse(status_code=404, content={"error": f"Spoke '{region}' not found"})

            staged.spokes.remove(spoke)
            # Removal is the direction that matters: without the write the spoke
            # came back at the next restart, sending traffic to a region an operator
            # had deliberately taken out of the topology.
            if error := await self._commit_topology(staged):
                return error
        return JSONResponse(content={"message": f"Spoke '{region}' removed"})

    async def update_spoke(self, request: Request) -> JSONResponse:
        """PUT /admin/regions/spokes/{region} — update spoke configuration."""
        if error := _platform_scope_error(request):
            return error
        region = request.path_params["region"]
        body = await request.json()
        async with self._topology_lock:
            staged = deepcopy(self.router.config)
            spoke = staged.get_spoke(region)
            if spoke is None:
                return JSONResponse(status_code=404, content={"error": f"Spoke '{region}' not found"})

            if "weight" in body:
                try:
                    spoke.weight = parse_topology_integer(
                        "weight",
                        body["weight"],
                    )
                except (TypeError, ValueError):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": (
                                "weight must be a non-negative integer"
                            )
                        },
                    )
            if "role" in body:
                try:
                    spoke.role = SpokeRole(body["role"])
                except ValueError:
                    return JSONResponse(status_code=400, content={"error": f"Invalid role: {body['role']}"})
            if "data_residency_zones" in body:
                spoke.data_residency_zones = body["data_residency_zones"]
            if "failover_priority" in body:
                spoke.failover_priority = int(body["failover_priority"])
            if "providers" in body:
                spoke.providers = body["providers"]
            if "models" in body:
                spoke.models = body["models"]

            if error := await self._commit_topology(staged):
                return error
        return JSONResponse(content={"message": f"Spoke '{region}' updated", "spoke": {
            "region": spoke.region, "role": spoke.role.value, "weight": spoke.weight,
            "data_residency_zones": spoke.data_residency_zones,
        }})

    async def update_config(self, request: Request) -> JSONResponse:
        """PUT /admin/regions/config — update hub-level topology settings."""
        if error := _platform_scope_error(request):
            return error
        body = await request.json()
        async with self._topology_lock:
            config = deepcopy(self.router.config)

            if "hub_region" in body:
                config.hub_region = body["hub_region"]
            if "data_residency_strict" in body:
                config.data_residency_strict = bool(body["data_residency_strict"])
            if "health_check_interval_seconds" in body:
                try:
                    config.health_check_interval_seconds = (
                        parse_topology_integer(
                            "health_check_interval_seconds",
                            body["health_check_interval_seconds"],
                        )
                    )
                except (TypeError, ValueError):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error": (
                                "health_check_interval_seconds must be "
                                "a positive integer"
                            )
                        },
                    )
            if "failover_threshold_consecutive" in body:
                config.failover_threshold_consecutive = int(body["failover_threshold_consecutive"])
            if "failover_cooldown_seconds" in body:
                config.failover_cooldown_seconds = int(body["failover_cooldown_seconds"])

            if error := await self._commit_topology(config):
                return error
        return JSONResponse(content={"message": "Topology config updated", "mode": self._detect_mode(config)})

    def _detect_mode(self, config) -> str:
        if len(config.spokes) <= 1:
            return "single_region"
        has_failover = any(s.role == SpokeRole.FAILOVER for s in config.spokes)
        if has_failover:
            return "active_passive"
        return "active_active"


def create_region_routes(region_api: RegionAPI) -> list[Route]:
    """Create Starlette routes for multi-region management."""
    return [
        Route("/admin/regions", region_api.get_topology, methods=["GET"]),
        Route("/admin/regions/config", region_api.update_config, methods=["PUT"]),
        Route("/admin/regions/health", region_api.get_health, methods=["GET"]),
        Route("/admin/regions/health/check", region_api.check_health_now, methods=["POST"]),
        Route("/admin/regions/route", region_api.route_test, methods=["POST"]),
        Route("/admin/regions/spokes", region_api.add_spoke, methods=["POST"]),
        Route("/admin/regions/spokes/{region}", region_api.update_spoke, methods=["PUT"]),
        Route("/admin/regions/spokes/{region}", region_api.remove_spoke, methods=["DELETE"]),
        Route("/admin/regions/failover", region_api.trigger_failover, methods=["POST"]),
        Route("/admin/regions/{region}/status", region_api.mark_spoke_status, methods=["PUT"]),
    ]
