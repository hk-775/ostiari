"""Tenant-scoped admin API routes for security event destinations."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.security.audit_trail import LEGACY_TENANT_ID
from src.gateway.security.event_dispatcher import (
    DestinationValidationError,
    DestinationType,
    EventDestination,
    SecurityEvent,
)
from src.gateway.persistence import PersistenceConflictError

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence
    from src.gateway.security.event_dispatcher import EventDispatcher

logger = logging.getLogger(__name__)

_READ_ONLY_ROLES = frozenset({"tenant_member", "tenant_auditor"})
_ADMIN_ROLES = frozenset({"admin", "tenant_admin", "platform_admin"})
_CANONICAL_ROLES = frozenset(
    {
        "tenant_admin",
        "tenant_member",
        "tenant_auditor",
        "platform_admin",
    }
)
_SECRET_CONFIG_KEYS = (
    "secret",
    "token",
    "password",
    "api_key",
    "credential",
    "authorization",
    "cookie",
    "header",
)
_TARGET_CONFIG_KEYS = (
    "url",
    "uri",
    "endpoint",
    "topic",
    "topic_arn",
    "log_group",
    "log_stream",
    "log_target",
    "destination",
    "target",
)


class WebhookStoreUnavailable(RuntimeError):
    """The tenant destination store cannot safely answer the operation."""


class TenantWebhookPersistence(Protocol):
    """Optional persistence contract for canonical tenant destinations."""

    enabled: bool

    async def save_event_destinations(
        self,
        destinations: list[dict],
        expected_revision: int,
    ) -> int:
        """Conditionally replace the legacy destination set."""
        ...

    async def load_event_destinations_snapshot(
        self,
    ) -> tuple[list[dict], int] | None:
        """Load the legacy destination set and revision."""
        ...

    async def save_tenant_event_destinations(
        self,
        tenant_id: str,
        destinations: list[dict],
        expected_revision: int,
    ) -> int:
        """Conditionally replace one tenant's full destination set."""
        ...

    async def load_tenant_event_destinations_snapshot(
        self,
        tenant_id: str,
    ) -> tuple[list[dict], int] | None:
        """Load one tenant's set and revision; ``None`` means never saved."""
        ...


class _TenantScopeError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _normalize_tenant_id(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise _TenantScopeError("tenant_id must be a non-empty string")
    return value


def _request_context(request: Request) -> object | None:
    state = getattr(request, "state", None)
    return getattr(state, "context", None)


def _context_roles(context: object | None) -> set[str]:
    raw_roles = getattr(context, "roles", None)
    if not isinstance(raw_roles, (list, tuple, set, frozenset)):
        return set()
    return {role for role in raw_roles if isinstance(role, str)}


def _request_tenant_id(
    request: Request,
    supplied_tenant_id: object = None,
) -> str:
    """Prefer authenticated scope and reject cross-tenant overrides."""
    supplied = _normalize_tenant_id(
        supplied_tenant_id if supplied_tenant_id is not None else request.query_params.get("tenant_id")
    )
    context = _request_context(request)
    authenticated = _normalize_tenant_id(getattr(context, "tenant_id", None))
    if authenticated is not None:
        if supplied is not None and supplied != authenticated:
            raise _TenantScopeError(
                "tenant_id does not match the authenticated tenant",
                status_code=403,
            )
        return authenticated
    if context is not None and (
        getattr(context, "principal_id", None) is not None
        or _context_roles(context) & _CANONICAL_ROLES
    ):
        raise _TenantScopeError(
            "authenticated tenant context is missing tenant_id",
        )
    return supplied or LEGACY_TENANT_ID


def _scope_error_response(exc: _TenantScopeError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "type": "invalid_tenant_scope",
                "message": str(exc),
            }
        },
    )


def _is_restricted_reader(request: Request) -> bool:
    context = _request_context(request)
    if context is None:
        return False
    roles = _context_roles(context)
    return bool(roles & _READ_ONLY_ROLES) and not bool(roles & _ADMIN_ROLES)


def _admin_safe_config(config: dict) -> dict:
    """Expose operational knobs without destination targets or credentials."""
    safe: dict = {}
    for key, value in config.items():
        lowered = str(key).lower()
        if any(part in lowered for part in _SECRET_CONFIG_KEYS):
            safe[f"{key}_configured"] = bool(value)
            continue
        if any(part in lowered for part in _TARGET_CONFIG_KEYS):
            safe[f"{key}_configured"] = bool(value)
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = value
    return safe


def _serialize_destination(
    destination: EventDestination,
    *,
    restricted: bool,
) -> dict:
    serialized = {
        "tenant_id": destination.tenant_id,
        "name": destination.name,
        "type": destination.destination_type.value,
        "enabled": destination.enabled,
    }
    if restricted:
        serialized["event_filter_configured"] = bool(destination.event_filter)
        serialized["config"] = {
            "configured": bool(destination.config),
        }
    else:
        serialized["event_filter"] = destination.event_filter
        serialized["config"] = _admin_safe_config(destination.config)
    return serialized


def _serialize_for_persistence(destination: EventDestination) -> dict:
    return {
        "tenant_id": destination.tenant_id,
        "name": destination.name,
        "destination_type": destination.destination_type.value,
        "config": destination.config,
        "event_filter": destination.event_filter,
        "enabled": destination.enabled,
    }


class WebhookAPI:
    """Manage durable event destinations within one authenticated tenant."""

    DESTINATION_SYNC_TTL_SECONDS = 5.0

    def __init__(
        self,
        dispatcher: EventDispatcher,
        persistence: (DynamoPersistence | TenantWebhookPersistence | None) = None,
    ) -> None:
        self.dispatcher = dispatcher
        self._persistence = persistence
        self._tenant_locks: dict[str, asyncio.Lock] = {}
        self._loaded_tenants: set[str] = set()
        self._tenant_revisions: dict[str, int] = {}
        self._last_tenant_refresh: dict[str, float] = {}
        self.dispatcher.set_destination_refresher(
            self._refresh_for_dispatch,
        )

    def _lock_for(self, tenant_id: str) -> asyncio.Lock:
        return self._tenant_locks.setdefault(tenant_id, asyncio.Lock())

    async def list_destinations(self, request: Request) -> JSONResponse:
        """GET /admin/webhooks?tenant_id="""
        try:
            tenant_id = _request_tenant_id(request)
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        try:
            async with self._lock_for(tenant_id):
                await self._ensure_loaded_locked(tenant_id, force=True)
                destinations = self.dispatcher.destinations_for_tenant(tenant_id)
        except WebhookStoreUnavailable:
            return self._unavailable_response(tenant_id)

        restricted = _is_restricted_reader(request)
        return JSONResponse(
            content={
                "tenant_id": tenant_id,
                "count": len(destinations),
                "destinations": [
                    _serialize_destination(
                        destination,
                        restricted=restricted,
                    )
                    for destination in destinations
                ],
                "stats": self.dispatcher.stats_for_tenant(tenant_id),
            }
        )

    async def add_destination(self, request: Request) -> JSONResponse:
        """POST /admin/webhooks"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid JSON body"},
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "JSON body must be an object"},
            )
        try:
            tenant_id = _request_tenant_id(request, body.get("tenant_id"))
        except _TenantScopeError as exc:
            return _scope_error_response(exc)

        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse(
                status_code=400,
                content={"error": "name is required"},
            )
        try:
            destination_type = DestinationType(body.get("type", "webhook"))
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": (f"Invalid type. Valid: {[item.value for item in DestinationType]}")},
            )

        config = body.get("config", {})
        event_filter = body.get("event_filter")
        if not isinstance(config, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "config must be an object"},
            )
        if event_filter is not None and (
            not isinstance(event_filter, list) or not all(isinstance(item, str) for item in event_filter)
        ):
            return JSONResponse(
                status_code=400,
                content={"error": "event_filter must be a list of strings"},
            )

        destination = EventDestination(
            tenant_id=tenant_id,
            name=name,
            destination_type=destination_type,
            config=config,
            event_filter=event_filter,
            enabled=bool(body.get("enabled", True)),
        )

        try:
            await self.dispatcher.validate_destination(destination)
        except DestinationValidationError as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "type": "invalid_destination",
                        "message": str(exc),
                    }
                },
            )

        try:
            async with self._lock_for(tenant_id):
                for attempt in range(2):
                    await self._ensure_loaded_locked(
                        tenant_id,
                        force=True,
                    )
                    current = self.dispatcher.destinations_for_tenant(
                        tenant_id
                    )
                    existed = any(item.name == name for item in current)
                    candidate = [
                        item for item in current if item.name != name
                    ]
                    candidate.append(destination)
                    try:
                        revision = await self._persist_candidate(
                            tenant_id,
                            candidate,
                        )
                    except PersistenceConflictError:
                        if attempt == 0:
                            continue
                        return self._write_conflict_response(tenant_id)
                    self._publish_candidate(
                        tenant_id,
                        candidate,
                        revision,
                    )
                    break
        except WebhookStoreUnavailable:
            return self._unavailable_response(tenant_id)

        return JSONResponse(
            status_code=200 if existed else 201,
            content={
                "tenant_id": tenant_id,
                "name": destination.name,
                "type": destination.destination_type.value,
                "enabled": destination.enabled,
                "event_filter": destination.event_filter,
                "status": "updated" if existed else "created",
            },
        )

    async def remove_destination(self, request: Request) -> JSONResponse:
        """DELETE /admin/webhooks/{name}?tenant_id="""
        try:
            tenant_id = _request_tenant_id(request)
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        name = request.path_params["name"]

        try:
            async with self._lock_for(tenant_id):
                for attempt in range(2):
                    await self._ensure_loaded_locked(
                        tenant_id,
                        force=True,
                    )
                    current = self.dispatcher.destinations_for_tenant(
                        tenant_id
                    )
                    candidate = [
                        item for item in current if item.name != name
                    ]
                    if len(candidate) == len(current):
                        return JSONResponse(
                            status_code=404,
                            content={
                                "error": (
                                    f"Destination '{name}' not found"
                                )
                            },
                        )
                    try:
                        revision = await self._persist_candidate(
                            tenant_id,
                            candidate,
                        )
                    except PersistenceConflictError:
                        if attempt == 0:
                            continue
                        return self._write_conflict_response(tenant_id)
                    self._publish_candidate(
                        tenant_id,
                        candidate,
                        revision,
                    )
                    break
        except WebhookStoreUnavailable:
            return self._unavailable_response(tenant_id)

        return JSONResponse(
            content={
                "tenant_id": tenant_id,
                "status": "removed",
                "name": name,
            }
        )

    async def test_destination(self, request: Request) -> JSONResponse:
        """POST /admin/webhooks/{name}/test?tenant_id="""
        try:
            tenant_id = _request_tenant_id(request)
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        name = request.path_params["name"]
        try:
            async with self._lock_for(tenant_id):
                await self._ensure_loaded_locked(tenant_id, force=True)
                destination = self.dispatcher.get_destination(
                    tenant_id,
                    name,
                )
        except WebhookStoreUnavailable:
            return self._unavailable_response(tenant_id)
        if destination is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"Destination '{name}' not found"},
            )

        event = SecurityEvent(
            event_id="test_event_001",
            event_type="test",
            timestamp=datetime.now(timezone.utc).isoformat(),
            tenant_id=tenant_id,
            severity="info",
            data={"message": "AxonLLM webhook test event"},
        )
        try:
            await self.dispatcher._send_to_destination(event, destination)
            return JSONResponse(
                content={
                    "tenant_id": tenant_id,
                    "status": "sent",
                    "destination": name,
                }
            )
        except Exception:
            logger.warning(
                "Destination test failed tenant=%s destination=%s",
                tenant_id,
                name,
            )
            return JSONResponse(
                status_code=502,
                content={
                    "tenant_id": tenant_id,
                    "status": "failed",
                    "destination": name,
                    "error": "Destination delivery failed",
                },
            )

    async def get_stats(self, request: Request) -> JSONResponse:
        """GET /admin/webhooks/stats?tenant_id="""
        try:
            tenant_id = _request_tenant_id(request)
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        try:
            async with self._lock_for(tenant_id):
                await self._ensure_loaded_locked(tenant_id)
        except WebhookStoreUnavailable:
            return self._unavailable_response(tenant_id)
        return JSONResponse(content=self.dispatcher.stats_for_tenant(tenant_id))

    async def _refresh_for_dispatch(self, tenant_id: str) -> None:
        async with self._lock_for(tenant_id):
            await self._ensure_loaded_locked(tenant_id)

    async def _ensure_loaded_locked(
        self,
        tenant_id: str,
        *,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if (
            not force
            and tenant_id in self._loaded_tenants
            and now - self._last_tenant_refresh.get(
                tenant_id,
                float("-inf"),
            )
            < self.DESTINATION_SYNC_TTL_SECONDS
        ):
            return
        persistence_enabled = (
            self._persistence is not None
            and getattr(self._persistence, "enabled", False)
        )
        if tenant_id == LEGACY_TENANT_ID and not persistence_enabled:
            self._loaded_tenants.add(tenant_id)
            self._tenant_revisions.setdefault(tenant_id, 0)
            self._last_tenant_refresh[tenant_id] = now
            return
        if not persistence_enabled:
            raise WebhookStoreUnavailable(
                "Tenant destination persistence is not configured"
            )
        loader_name = (
            "load_event_destinations_snapshot"
            if tenant_id == LEGACY_TENANT_ID
            else "load_tenant_event_destinations_snapshot"
        )
        loader = getattr(
            self._persistence,
            loader_name,
            None,
        )
        if loader is None:
            raise WebhookStoreUnavailable(
                "Tenant destination loading is not configured"
            )
        try:
            snapshot = (
                await loader()
                if tenant_id == LEGACY_TENANT_ID
                else await loader(tenant_id)
            )
        except Exception as exc:
            raise WebhookStoreUnavailable(
                "Tenant destination loading failed"
            ) from exc
        if snapshot is None and tenant_id == LEGACY_TENANT_ID:
            # No operator-authored legacy set exists yet. Keep bootstrap/config
            # destinations as the seed for the first conditional write.
            self._loaded_tenants.add(tenant_id)
            self._tenant_revisions[tenant_id] = 0
            self._last_tenant_refresh[tenant_id] = now
            return
        rows, revision = snapshot if snapshot is not None else ([], 0)
        if (
            not isinstance(rows, list)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
        ):
            raise WebhookStoreUnavailable(
                "Tenant destination persistence returned invalid data"
            )

        destinations: list[EventDestination] = []
        try:
            for row in rows:
                row_tenant_id = row.get("tenant_id", tenant_id)
                if row_tenant_id != tenant_id:
                    raise ValueError("cross-tenant destination row")
                destinations.append(
                    EventDestination(
                        tenant_id=tenant_id,
                        name=row["name"],
                        destination_type=DestinationType(
                            row.get("destination_type", "webhook")
                        ),
                        config=row.get("config", {}),
                        event_filter=row.get("event_filter"),
                        enabled=bool(row.get("enabled", True)),
                    )
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise WebhookStoreUnavailable(
                "Tenant destination persistence returned invalid data"
            ) from exc
        self.dispatcher.replace_destinations(tenant_id, destinations)
        self._loaded_tenants.add(tenant_id)
        self._tenant_revisions[tenant_id] = revision
        self._last_tenant_refresh[tenant_id] = now

    async def _persist_candidate(
        self,
        tenant_id: str,
        destinations: list[EventDestination],
    ) -> int:
        serialized = [
            _serialize_for_persistence(destination)
            for destination in destinations
        ]
        persistence_enabled = (
            self._persistence is not None
            and getattr(self._persistence, "enabled", False)
        )
        if tenant_id == LEGACY_TENANT_ID and not persistence_enabled:
            return self._tenant_revisions.get(tenant_id, 0) + 1
        if not persistence_enabled:
            raise WebhookStoreUnavailable(
                "Tenant destination persistence is not configured"
            )
        saver_name = (
            "save_event_destinations"
            if tenant_id == LEGACY_TENANT_ID
            else "save_tenant_event_destinations"
        )
        saver = getattr(
            self._persistence,
            saver_name,
            None,
        )
        if saver is None:
            raise WebhookStoreUnavailable(
                "Tenant destination persistence is not configured"
            )
        expected_revision = self._tenant_revisions.get(tenant_id)
        if expected_revision is None:
            raise WebhookStoreUnavailable(
                "Tenant destination revision is unavailable"
            )
        try:
            revision = (
                await saver(serialized, expected_revision)
                if tenant_id == LEGACY_TENANT_ID
                else await saver(
                    tenant_id,
                    serialized,
                    expected_revision,
                )
            )
        except PersistenceConflictError:
            raise
        except Exception as exc:
            raise WebhookStoreUnavailable(
                "Tenant destination persistence failed"
            ) from exc
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision <= expected_revision
        ):
            raise WebhookStoreUnavailable(
                "Tenant destination persistence returned invalid revision"
            )
        return revision

    def _publish_candidate(
        self,
        tenant_id: str,
        destinations: list[EventDestination],
        revision: int,
    ) -> None:
        self.dispatcher.replace_destinations(tenant_id, destinations)
        self._tenant_revisions[tenant_id] = revision
        self._last_tenant_refresh[tenant_id] = time.monotonic()

    @staticmethod
    def _write_conflict_response(tenant_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "tenant_id": tenant_id,
                "error": {
                    "type": "write_conflict",
                    "code": "webhook_write_conflict",
                    "message": (
                        "Webhook configuration changed concurrently; "
                        "retry the edit"
                    ),
                },
            },
        )

    @staticmethod
    def _unavailable_response(tenant_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "tenant_id": tenant_id,
                "error": {
                    "type": "webhook_store_unavailable",
                    "message": ("Tenant webhook configuration is temporarily unavailable."),
                },
            },
        )


def create_webhook_routes(webhook_api: WebhookAPI) -> list[Route]:
    """Create Starlette routes for webhook/event dispatcher management."""
    return [
        Route(
            "/admin/webhooks",
            webhook_api.list_destinations,
            methods=["GET"],
        ),
        Route(
            "/admin/webhooks",
            webhook_api.add_destination,
            methods=["POST"],
        ),
        Route(
            "/admin/webhooks/stats",
            webhook_api.get_stats,
            methods=["GET"],
        ),
        Route(
            "/admin/webhooks/{name}",
            webhook_api.remove_destination,
            methods=["DELETE"],
        ),
        Route(
            "/admin/webhooks/{name}/test",
            webhook_api.test_destination,
            methods=["POST"],
        ),
    ]
