"""Durable provider route configuration and gateway distribution."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org, require_role
from control_plane.database import get_db
from control_plane.models.database import Gateway, ProviderRouteRecord
from control_plane.routers.providers import _decrypt, _encrypt, _providers
from control_plane.services.audit_service import actor_of, audit
from control_plane.services.push_service import gateway_config_headers

log = logging.getLogger("control_plane.routers.provider_routes")

router = APIRouter(prefix="/api/providers", tags=["provider-routes"])

_ROUTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER_ALIASES = {
    "azure": "azure_openai",
    "google": "google_ai",
    "vertex": "vertex_ai",
}
_PROVIDERS = {
    "ai21",
    "anthropic",
    "azure_openai",
    "bedrock",
    "bedrock-mantle",
    "cohere",
    "fireworks",
    "google_ai",
    "groq",
    "openai",
    "together",
    "vertex_ai",
    "xai",
}
_AUTH_TYPES = {
    "api_key",
    "aws_credentials",
    "azure_key",
    "gcp_service_account",
}
_CREDENTIAL_KEYS = {
    "api_key",
    "access_key",
    "secret_key",
    "session_token",
    "access_token",
    "region",
}
_RUNTIME_PUBLIC_FIELDS = frozenset({
    "route_id",
    "provider",
    "endpoint",
    "auth_type",
    "region",
    "allowed_models",
    "weight",
    "adaptive_weight",
    "priority",
    "enabled",
    "max_concurrency",
    "capacity_group",
    "capacity_limit",
    "connect_timeout",
    "read_timeout",
    "max_connections",
    "max_connections_per_host",
    "keepalive_timeout",
    "has_credentials",
    "status",
    "inflight",
    "selected",
    "successes",
    "failures",
    "error_ewma",
    "latency_ewma_ms",
    "latency_per_token_ewma_ms",
    "cooldown_remaining_seconds",
    "last_status_code",
})


def canonical_provider(name: str) -> str:
    normalized = name.strip().lower()
    return _PROVIDER_ALIASES.get(normalized, normalized)


def _default_auth_type(provider: str) -> str:
    if provider in {"bedrock", "bedrock-mantle"}:
        return "aws_credentials"
    if provider == "azure_openai":
        return "azure_key"
    if provider == "vertex_ai":
        return "gcp_service_account"
    return "api_key"


def _validate_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    if not endpoint:
        return ""
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("endpoint must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("endpoint must not contain userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("endpoint must not contain a query or fragment")
    return endpoint


def _validate_credentials(value: dict[str, str]) -> dict[str, str]:
    unknown = sorted(set(value) - _CREDENTIAL_KEYS)
    if unknown:
        raise ValueError(f"unsupported credential fields: {', '.join(unknown)}")
    return {
        str(key): str(item)
        for key, item in value.items()
        if item is not None and str(item)
    }


class ProviderRouteCreate(BaseModel):
    model_config = {"extra": "forbid"}

    route_id: str = Field(min_length=1, max_length=128)
    endpoint: str = ""
    auth_type: str = ""
    credentials: dict[str, str] = Field(default_factory=dict)
    region: str = ""
    allowed_models: list[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, gt=0)
    priority: int = Field(default=0, ge=0)
    enabled: bool = True
    max_concurrency: int = Field(default=100, gt=0)
    capacity_group: str = Field(default="", max_length=128)
    capacity_limit: int = Field(default=0, ge=0)
    connect_timeout: float = Field(default=30.0, gt=0)
    read_timeout: float = Field(default=120.0, gt=0)
    max_connections: int = Field(default=100, gt=0)
    max_connections_per_host: int = Field(default=100, gt=0)
    keepalive_timeout: float = Field(default=30.0, gt=0)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_params: dict[str, str] = Field(default_factory=dict)

    @field_validator("route_id")
    @classmethod
    def validate_route_id(cls, value: str) -> str:
        if not _ROUTE_ID_RE.fullmatch(value):
            raise ValueError(
                "route_id must start with an alphanumeric character and contain "
                "only letters, numbers, '.', '_', ':', or '-'"
            )
        return value

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return _validate_endpoint(value)

    @field_validator("credentials")
    @classmethod
    def validate_credentials(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_credentials(value)

    @field_validator("allowed_models")
    @classmethod
    def normalize_models(cls, value: list[str]) -> list[str]:
        return sorted({item.strip() for item in value if item.strip()})


class ProviderRouteUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    endpoint: str | None = None
    auth_type: str | None = None
    credentials: dict[str, str] | None = None
    region: str | None = None
    allowed_models: list[str] | None = None
    weight: float | None = Field(default=None, gt=0)
    priority: int | None = Field(default=None, ge=0)
    enabled: bool | None = None
    max_concurrency: int | None = Field(default=None, gt=0)
    capacity_group: str | None = Field(default=None, max_length=128)
    capacity_limit: int | None = Field(default=None, ge=0)
    connect_timeout: float | None = Field(default=None, gt=0)
    read_timeout: float | None = Field(default=None, gt=0)
    max_connections: int | None = Field(default=None, gt=0)
    max_connections_per_host: int | None = Field(default=None, gt=0)
    keepalive_timeout: float | None = Field(default=None, gt=0)
    extra_headers: dict[str, str] | None = None
    extra_params: dict[str, str] | None = None

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str | None) -> str | None:
        return None if value is None else _validate_endpoint(value)

    @field_validator("credentials")
    @classmethod
    def validate_credentials(
        cls, value: dict[str, str] | None
    ) -> dict[str, str] | None:
        return None if value is None else _validate_credentials(value)

    @field_validator("allowed_models")
    @classmethod
    def normalize_models(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return sorted({item.strip() for item in value if item.strip()})


class ProviderRouteResponse(BaseModel):
    route_id: str
    provider: str
    endpoint: str
    auth_type: str
    region: str
    allowed_models: list[str]
    weight: float
    priority: int
    enabled: bool
    max_concurrency: int
    capacity_group: str
    capacity_limit: int
    connect_timeout: float
    read_timeout: float
    max_connections: int
    max_connections_per_host: int
    keepalive_timeout: float
    has_credentials: bool
    has_custom_headers: bool
    has_extra_params: bool
    created_at: datetime
    updated_at: datetime


def _private_document(
    *,
    credentials: dict[str, str],
    extra_headers: dict[str, str],
    extra_params: dict[str, str],
) -> dict[str, dict[str, str]]:
    return {
        "credentials": credentials,
        "extra_headers": {
            str(key): str(value) for key, value in extra_headers.items()
        },
        "extra_params": {
            str(key): str(value) for key, value in extra_params.items()
        },
    }


def _encrypt_private(document: dict[str, Any]) -> str:
    return _encrypt(json.dumps(document, sort_keys=True, separators=(",", ":")))


def _decrypt_private(record: ProviderRouteRecord) -> dict[str, Any]:
    if not record.private_config_encrypted:
        return _private_document(
            credentials={},
            extra_headers={},
            extra_params={},
        )
    try:
        value = json.loads(_decrypt(record.private_config_encrypted))
    except Exception as exc:
        raise ValueError(
            f"private config for route '{record.route_id}' cannot be decrypted"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(
            f"private config for route '{record.route_id}' is invalid"
        )
    return value


def _to_response(record: ProviderRouteRecord) -> ProviderRouteResponse:
    try:
        private = _decrypt_private(record)
    except ValueError:
        private = {}
    return ProviderRouteResponse(
        route_id=record.route_id,
        provider=record.provider,
        endpoint=record.endpoint,
        auth_type=record.auth_type,
        region=record.region,
        allowed_models=list(record.allowed_models or []),
        weight=record.weight,
        priority=record.priority,
        enabled=record.enabled,
        max_concurrency=record.max_concurrency,
        capacity_group=record.capacity_group,
        capacity_limit=record.capacity_limit,
        connect_timeout=record.connect_timeout,
        read_timeout=record.read_timeout,
        max_connections=record.max_connections,
        max_connections_per_host=record.max_connections_per_host,
        keepalive_timeout=record.keepalive_timeout,
        has_credentials=bool(private.get("credentials")),
        has_custom_headers=bool(private.get("extra_headers")),
        has_extra_params=bool(private.get("extra_params")),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _public_details(record: ProviderRouteRecord) -> dict[str, Any]:
    return {
        "provider": record.provider,
        "endpoint": record.endpoint,
        "auth_type": record.auth_type,
        "region": record.region,
        "allowed_models": list(record.allowed_models or []),
        "weight": record.weight,
        "priority": record.priority,
        "enabled": record.enabled,
        "max_concurrency": record.max_concurrency,
        "capacity_group": record.capacity_group,
        "capacity_limit": record.capacity_limit,
    }


async def _find_route(
    db: AsyncSession,
    org: str,
    provider: str,
    route_id: str,
) -> ProviderRouteRecord | None:
    return (
        await db.execute(
            select(ProviderRouteRecord).where(
                ProviderRouteRecord.org_id == org,
                ProviderRouteRecord.provider == provider,
                ProviderRouteRecord.route_id == route_id,
            )
        )
    ).scalar_one_or_none()


def _provider_is_configured(org: str, provider: str) -> bool:
    return any(
        canonical_provider(name) == provider
        for name in _providers[org]
    )


def _runtime_document(
    record: ProviderRouteRecord,
    private: dict[str, Any],
) -> dict[str, Any]:
    return {
        "route_id": record.route_id,
        "provider": record.provider,
        "endpoint": record.endpoint,
        "auth_type": record.auth_type,
        "credentials": private.get("credentials") or {},
        "region": record.region,
        "allowed_models": list(record.allowed_models or []),
        "weight": record.weight,
        "priority": record.priority,
        "enabled": record.enabled,
        "max_concurrency": record.max_concurrency,
        "capacity_group": record.capacity_group,
        "capacity_limit": record.capacity_limit,
        "connect_timeout": record.connect_timeout,
        "read_timeout": record.read_timeout,
        "max_connections": record.max_connections,
        "max_connections_per_host": record.max_connections_per_host,
        "keepalive_timeout": record.keepalive_timeout,
        "extra_headers": private.get("extra_headers") or {},
        "extra_params": private.get("extra_params") or {},
    }


def public_runtime_route_catalog(
    routes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return route metadata suitable for operator-facing responses."""
    public: list[dict[str, Any]] = []
    for raw in routes:
        route = {
            key: value
            for key, value in raw.items()
            if key in _RUNTIME_PUBLIC_FIELDS
        }
        route["has_credentials"] = bool(raw.get("credentials"))
        route["has_custom_headers"] = bool(raw.get("extra_headers"))
        route["has_extra_params"] = bool(raw.get("extra_params"))
        public.append(route)
    return public


def _legacy_runtime_route(
    provider_name: str,
    record: Any,
) -> dict[str, Any] | None:
    provider = canonical_provider(provider_name)
    if provider not in _PROVIDERS or not record.enabled:
        return None
    try:
        api_key = _decrypt(record.api_key_encrypted)
    except Exception as exc:
        raise ValueError(
            f"legacy credentials for provider '{provider_name}' cannot be decrypted"
        ) from exc

    credentials: dict[str, str] = {}
    extra_params: dict[str, str] = {}
    if provider in {"bedrock", "bedrock-mantle"}:
        if record.region:
            credentials["region"] = record.region
    elif provider == "vertex_ai":
        if api_key:
            credentials["access_token"] = api_key
        if record.project_id:
            extra_params["project"] = record.project_id
        if record.region:
            extra_params["location"] = record.region
    elif api_key:
        credentials["api_key"] = api_key
    else:
        return None

    return {
        "route_id": f"{provider}:default",
        "provider": provider,
        "endpoint": record.api_base_url,
        "auth_type": _default_auth_type(provider),
        "credentials": credentials,
        "region": record.region,
        "allowed_models": [],
        "weight": 1.0,
        "priority": 0,
        "enabled": True,
        "max_concurrency": 100,
        "capacity_group": "",
        "capacity_limit": 0,
        "connect_timeout": 30.0,
        "read_timeout": 120.0,
        "max_connections": 100,
        "max_connections_per_host": 100,
        "keepalive_timeout": 30.0,
        "extra_headers": {},
        "extra_params": extra_params,
    }


async def runtime_route_catalog(
    db: AsyncSession,
    org: str,
) -> list[dict[str, Any]]:
    """Build the complete runtime catalog, including legacy default routes."""
    records = (
        await db.execute(
            select(ProviderRouteRecord)
            .where(ProviderRouteRecord.org_id == org)
            .order_by(
                ProviderRouteRecord.provider,
                ProviderRouteRecord.priority,
                ProviderRouteRecord.route_id,
            )
        )
    ).scalars().all()
    explicit_providers = {record.provider for record in records}
    disabled_providers = {
        canonical_provider(name)
        for name, record in _providers[org].items()
        if not record.enabled
    }
    runtime = [
        _runtime_document(record, _decrypt_private(record))
        for record in records
        if record.enabled and record.provider not in disabled_providers
    ]
    for provider_name, record in _providers[org].items():
        provider = canonical_provider(provider_name)
        if provider in explicit_providers:
            continue
        route = _legacy_runtime_route(provider_name, record)
        if route is not None:
            runtime.append(route)
    return sorted(
        runtime,
        key=lambda item: (
            item["provider"],
            item["priority"],
            item["route_id"],
        ),
    )


@router.get("/routes", response_model=list[ProviderRouteResponse])
async def list_routes(
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> list[ProviderRouteResponse]:
    records = (
        await db.execute(
            select(ProviderRouteRecord)
            .where(ProviderRouteRecord.org_id == org)
            .order_by(
                ProviderRouteRecord.provider,
                ProviderRouteRecord.priority,
                ProviderRouteRecord.route_id,
            )
        )
    ).scalars().all()
    return [_to_response(record) for record in records]


@router.get("/routes/runtime")
async def route_runtime(
    _user=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict[str, Any]:
    """Collect secret-free route health from every tenant gateway."""
    gateways = (
        await db.execute(select(Gateway).where(Gateway.org_id == org))
    ).scalars().all()
    async with httpx.AsyncClient(
        timeout=5.0,
        headers=gateway_config_headers(),
    ) as client:
        async def collect(gateway: Gateway) -> dict[str, Any]:
            try:
                response = await client.get(
                    f"{gateway.endpoint}/config/provider-routes"
                )
                if response.status_code != 200:
                    return {
                        "gateway_id": gateway.id,
                        "status": "error",
                        "routes": [],
                        "error": f"HTTP {response.status_code}",
                    }
                body = response.json()
                routes = body.get("routes", []) if isinstance(body, dict) else []
                sanitized = [
                    {
                        key: value
                        for key, value in route.items()
                        if key in _RUNTIME_PUBLIC_FIELDS
                    }
                    for route in routes
                    if isinstance(route, dict)
                ]
                return {
                    "gateway_id": gateway.id,
                    "status": "ok",
                    "routes": sanitized,
                    "error": "",
                }
            except (httpx.HTTPError, ValueError) as exc:
                return {
                    "gateway_id": gateway.id,
                    "status": "error",
                    "routes": [],
                    "error": str(exc),
                }

        snapshots = list(await asyncio.gather(
            *(collect(gateway) for gateway in gateways)
        ))
    return {
        "gateways": len(gateways),
        "reachable": sum(
            1 for snapshot in snapshots if snapshot["status"] == "ok"
        ),
        "snapshots": snapshots,
    }


@router.post(
    "/{provider}/routes",
    response_model=ProviderRouteResponse,
    status_code=201,
)
async def create_route(
    provider: str,
    body: ProviderRouteCreate,
    request: Request,
    _user=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> ProviderRouteResponse:
    provider = canonical_provider(provider)
    if provider not in _PROVIDERS:
        raise HTTPException(status_code=422, detail="unsupported provider")
    if not _provider_is_configured(org, provider):
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider}' is not configured",
        )
    auth_type = body.auth_type or _default_auth_type(provider)
    if auth_type not in _AUTH_TYPES:
        raise HTTPException(status_code=422, detail="unsupported auth_type")

    now = datetime.now(timezone.utc)
    record = ProviderRouteRecord(
        org_id=org,
        route_id=body.route_id,
        provider=provider,
        endpoint=body.endpoint,
        auth_type=auth_type,
        private_config_encrypted=_encrypt_private(
            _private_document(
                credentials=body.credentials,
                extra_headers=body.extra_headers,
                extra_params=body.extra_params,
            )
        ),
        region=body.region,
        allowed_models=body.allowed_models,
        weight=body.weight,
        priority=body.priority,
        enabled=body.enabled,
        max_concurrency=body.max_concurrency,
        capacity_group=body.capacity_group,
        capacity_limit=body.capacity_limit,
        connect_timeout=body.connect_timeout,
        read_timeout=body.read_timeout,
        max_connections=body.max_connections,
        max_connections_per_host=body.max_connections_per_host,
        keepalive_timeout=body.keepalive_timeout,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Route '{body.route_id}' already exists",
        ) from exc
    await audit.log(
        db,
        actor_of(request),
        "create",
        "provider_route",
        body.route_id,
        _public_details(record),
        org=org,
    )
    await db.commit()
    return _to_response(record)


@router.put(
    "/{provider}/routes/{route_id}",
    response_model=ProviderRouteResponse,
)
async def update_route(
    provider: str,
    route_id: str,
    body: ProviderRouteUpdate,
    request: Request,
    _user=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> ProviderRouteResponse:
    provider = canonical_provider(provider)
    record = await _find_route(db, org, provider, route_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Provider route not found")
    before = _public_details(record)
    previous_auth_type = record.auth_type

    public_fields = {
        "endpoint",
        "auth_type",
        "region",
        "allowed_models",
        "weight",
        "priority",
        "enabled",
        "max_concurrency",
        "capacity_group",
        "capacity_limit",
        "connect_timeout",
        "read_timeout",
        "max_connections",
        "max_connections_per_host",
        "keepalive_timeout",
    }
    for field in body.model_fields_set.intersection(public_fields):
        value = getattr(body, field)
        if value is None:
            raise HTTPException(
                status_code=422,
                detail=f"{field} cannot be null",
            )
        if field == "auth_type" and value not in _AUTH_TYPES:
            raise HTTPException(status_code=422, detail="unsupported auth_type")
        setattr(record, field, value)

    private = _decrypt_private(record)
    credentials_cleared = False
    if (
        "auth_type" in body.model_fields_set
        and record.auth_type != previous_auth_type
        and "credentials" not in body.model_fields_set
    ):
        private["credentials"] = {}
        credentials_cleared = True
    for field in {"credentials", "extra_headers", "extra_params"}:
        if field in body.model_fields_set:
            private[field] = getattr(body, field) or {}
    record.private_config_encrypted = _encrypt_private(private)
    record.updated_at = datetime.now(timezone.utc)
    await db.flush()

    after = _public_details(record)
    changes = {
        key: {"from": before[key], "to": after[key]}
        for key in after
        if before[key] != after[key]
    }
    for field in {"credentials", "extra_headers", "extra_params"}:
        if field in body.model_fields_set or (
            field == "credentials" and credentials_cleared
        ):
            changes[field] = {"changed": True}
    await audit.log(
        db,
        actor_of(request),
        "update",
        "provider_route",
        route_id,
        changes,
        org=org,
    )
    await db.commit()
    return _to_response(record)


@router.delete("/{provider}/routes/{route_id}")
async def delete_route(
    provider: str,
    route_id: str,
    request: Request,
    _user=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict[str, str]:
    provider = canonical_provider(provider)
    record = await _find_route(db, org, provider, route_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Provider route not found")
    details = _public_details(record)
    await db.delete(record)
    await audit.log(
        db,
        actor_of(request),
        "delete",
        "provider_route",
        route_id,
        details,
        org=org,
    )
    await db.commit()
    return {"deleted": route_id}


async def push_runtime_routes(
    db: AsyncSession,
    org: str,
) -> dict[str, Any]:
    routes = await runtime_route_catalog(db, org)
    gateways = (
        await db.execute(select(Gateway).where(Gateway.org_id == org))
    ).scalars().all()
    async with httpx.AsyncClient(
        timeout=10.0,
        headers=gateway_config_headers(),
    ) as client:
        async def push(gateway: Gateway) -> dict[str, Any]:
            try:
                health = await client.get(f"{gateway.endpoint}/health")
                if health.status_code == 200:
                    try:
                        health_body = health.json()
                    except ValueError:
                        health_body = {}
                    modules = (
                        health_body.get("modules_active")
                        if isinstance(health_body, dict)
                        else None
                    )
                    if isinstance(modules, list) and "llm_gateway" not in modules:
                        return {
                            "gateway_id": gateway.id,
                            "pushed": False,
                            "skipped": True,
                            "detail": "LLM gateway module is not active",
                        }
                response = await client.post(
                    f"{gateway.endpoint}/config/provider-routes",
                    json={"routes": routes},
                )
                if response.status_code == 200:
                    try:
                        detail: Any = response.json()
                    except ValueError:
                        detail = {"status": "applied"}
                else:
                    detail = response.text[:200]
                return {
                    "gateway_id": gateway.id,
                    "pushed": response.status_code == 200,
                    "skipped": False,
                    "detail": detail,
                }
            except httpx.HTTPError as exc:
                return {
                    "gateway_id": gateway.id,
                    "pushed": False,
                    "skipped": False,
                    "detail": str(exc),
                }

        results = list(await asyncio.gather(
            *(push(gateway) for gateway in gateways)
        ))
    return {
        "routes": len(routes),
        "gateways": len(gateways),
        "pushed": sum(1 for item in results if item["pushed"]),
        "failed": sum(
            1
            for item in results
            if not item["pushed"] and not item.get("skipped")
        ),
        "skipped": sum(1 for item in results if item.get("skipped")),
        "results": results,
    }


@router.post("/routes/push")
async def push_routes(
    request: Request,
    _user=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict[str, Any]:
    try:
        result = await push_runtime_routes(db, org)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit.log(
        db,
        actor_of(request),
        "push",
        "provider_routes",
        "*",
        {
            key: result[key]
            for key in ("routes", "gateways", "pushed", "failed", "skipped")
        },
        org=org,
    )
    await db.commit()
    return result
