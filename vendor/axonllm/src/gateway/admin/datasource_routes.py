"""Tenant administration for platform-bound Athena datasources."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.auth.project_repository import ProjectStoreUnavailable
from src.gateway.models import TenantRole
from src.gateway.query.models import (
    AthenaDatasource,
    AthenaRoleBindings,
    QueryConfigurationError,
)
from src.gateway.query.repository import (
    DatasourceConflictError,
    DatasourceCursorError,
    DatasourceQuotaExceededError,
    DatasourceRepository,
    DatasourceStoreUnavailable,
)
from src.gateway.security.audit_trail import AuditEventType, AuditTrail


_MAX_BODY_BYTES = 64 * 1024
_READ_ONLY_ROLES = frozenset(
    {TenantRole.TENANT_MEMBER, TenantRole.TENANT_AUDITOR}
)


class _RequestBodyLimitExceeded(Exception):
    """Signal a bounded read failure without masking stream errors."""


class DatasourceAuditUnavailable(RuntimeError):
    """Raised when a mutation cannot be durably audited."""


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "type": "datasource_error",
                "code": code,
                "message": message,
            }
        },
    )


async def _read_request_body(request: Request) -> bytes:
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(chunk) > _MAX_BODY_BYTES - len(body):
                raise _RequestBodyLimitExceeded
            body.extend(chunk)
    except _RequestBodyLimitExceeded as exc:
        raise QueryConfigurationError(
            "datasource request exceeds 64 KiB"
        ) from exc
    except Exception as exc:
        raise QueryConfigurationError(
            "datasource request body could not be read"
        ) from exc
    return bytes(body)


async def _json_object(request: Request) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            length = int(content_length)
        except ValueError as exc:
            raise QueryConfigurationError(
                "Content-Length is invalid"
            ) from exc
        if length < 0 or length > _MAX_BODY_BYTES:
            raise QueryConfigurationError(
                "datasource request exceeds 64 KiB"
            )
    body = await _read_request_body(request)

    def _reject_duplicates(
        pairs: list[tuple[str, Any]],
    ) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise QueryConfigurationError(
                    f"datasource request contains duplicate field {key!r}"
                )
            value[key] = item
        return value

    def _reject_constant(value: str) -> None:
        raise QueryConfigurationError(
            f"datasource request contains non-finite number {value}"
        )

    try:
        value = json.loads(
            body,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except QueryConfigurationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QueryConfigurationError(
            "datasource request must be valid JSON"
        ) from exc
    if not isinstance(value, dict):
        raise QueryConfigurationError(
            "datasource request must be a JSON object"
        )
    return value


def _positive_revision(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise QueryConfigurationError(
            "expected_revision must be a positive integer"
        )
    return value


def _request_id(request: Request) -> str:
    value = request.headers.get("x-request-id")
    if value is None:
        return f"ds_{uuid.uuid4().hex}"
    if (
        not value
        or value != value.strip()
        or len(value) > 128
        or any(ord(character) < 32 for character in value)
    ):
        raise QueryConfigurationError("X-Request-Id is invalid")
    return value


def _redacted_changes(
    current: AthenaDatasource | None,
    candidate: AthenaDatasource | None,
) -> dict[str, Any]:
    fields = (
        "name",
        "region",
        "catalog",
        "database",
        "workgroup",
        "enabled",
        "role_arn",
    )
    changes: dict[str, Any] = {}
    for field_name in fields:
        previous = (
            getattr(current, field_name) if current is not None else None
        )
        proposed = (
            getattr(candidate, field_name)
            if candidate is not None
            else None
        )
        if previous == proposed:
            continue
        if field_name == "role_arn":
            changes["role_binding"] = {
                "changed": True,
                "previous_sha256": (
                    hashlib.sha256(previous.encode("utf-8")).hexdigest()
                    if previous is not None
                    else None
                ),
                "proposed_sha256": (
                    hashlib.sha256(proposed.encode("utf-8")).hexdigest()
                    if proposed is not None
                    else None
                ),
            }
            continue
        changes[field_name] = {
            "previous": previous,
            "proposed": proposed,
        }
    return changes


class DatasourceAPI:
    """Manage query metadata while deployment bindings retain role authority."""

    def __init__(
        self,
        *,
        repository: DatasourceRepository,
        bindings: AthenaRoleBindings,
        project_resolver: Any,
        audit_trail: AuditTrail | None = None,
        require_durable_audit: bool = True,
    ) -> None:
        self.repository = repository
        self.bindings = bindings
        self.project_resolver = project_resolver
        self.audit_trail = audit_trail
        self.require_durable_audit = require_durable_audit

    async def _audit_mutation(
        self,
        *,
        event_type: AuditEventType,
        principal: object,
        tenant_id: str,
        project_id: str,
        request_id: str,
        data: dict[str, Any],
    ) -> None:
        if self.audit_trail is None or (
            self.require_durable_audit
            and not self.audit_trail.durable_enabled
        ):
            raise DatasourceAuditUnavailable(
                "Durable datasource audit is unavailable."
            )
        try:
            await self.audit_trail.record(
                event_type=event_type,
                user_id=getattr(principal, "principal_id"),
                project_id=project_id,
                request_id=request_id,
                data=data,
                tenant_id=tenant_id,
            )
        except Exception as exc:
            raise DatasourceAuditUnavailable(
                "Durable datasource audit is unavailable."
            ) from exc

    @staticmethod
    def _scope(request: Request) -> tuple[str, object]:
        principal = getattr(request.state, "principal", None)
        context = getattr(request.state, "context", None)
        tenant_id = getattr(context, "tenant_id", None)
        if principal is None or not isinstance(tenant_id, str) or not tenant_id:
            raise QueryConfigurationError(
                "canonical tenant identity is required"
            )
        return tenant_id, principal

    async def _require_project(
        self,
        tenant_id: str,
        project_id: object,
    ) -> str:
        if (
            not isinstance(project_id, str)
            or not project_id
            or project_id != project_id.strip()
        ):
            raise QueryConfigurationError(
                "project_id must be a non-empty string"
            )
        if self.project_resolver is None:
            raise DatasourceStoreUnavailable(
                "project authority is unavailable"
            )
        try:
            project = await self.project_resolver.resolve(
                tenant_id,
                project_id,
            )
        except ProjectStoreUnavailable as exc:
            raise DatasourceStoreUnavailable(
                "project authority is unavailable"
            ) from exc
        if project is None:
            raise LookupError("project not found")
        return project_id

    @staticmethod
    def _restricted(principal: object) -> bool:
        roles = getattr(principal, "roles", frozenset())
        return bool(set(roles) & _READ_ONLY_ROLES) and (
            TenantRole.TENANT_ADMIN not in roles
            and TenantRole.PLATFORM_ADMIN not in roles
        )

    async def list_datasources(
        self,
        request: Request,
    ) -> JSONResponse:
        try:
            tenant_id, principal = self._scope(request)
            raw_project = request.query_params.get("project_id")
            project_id = (
                await self._require_project(tenant_id, raw_project)
                if raw_project is not None
                else None
            )
            raw_limit = request.query_params.get("limit", "50")
            try:
                limit = int(raw_limit)
            except ValueError as exc:
                raise QueryConfigurationError(
                    "limit must be an integer between 1 and 100"
                ) from exc
            if not 1 <= limit <= 100:
                raise QueryConfigurationError(
                    "limit must be between 1 and 100"
                )
            page = await self.repository.list(
                tenant_id,
                project_id=project_id,
                limit=limit,
                cursor=request.query_params.get("cursor"),
            )
        except (QueryConfigurationError, DatasourceCursorError) as exc:
            return _error(400, "invalid_datasource_request", str(exc))
        except LookupError:
            return _error(404, "resource_not_found", "Resource not found.")
        except DatasourceStoreUnavailable:
            return _error(
                503,
                "datasource_store_unavailable",
                "Datasource configuration is temporarily unavailable.",
            )
        restricted = self._restricted(principal)
        return JSONResponse(
            {
                "tenant_id": tenant_id,
                "count": len(page.items),
                "datasources": [
                    item.to_dict(include_role=not restricted)
                    for item in page.items
                ],
                "next_cursor": page.next_cursor,
            }
        )

    async def get_datasource(
        self,
        request: Request,
    ) -> JSONResponse:
        try:
            tenant_id, principal = self._scope(request)
            project_id = await self._require_project(
                tenant_id,
                request.query_params.get("project_id"),
            )
            datasource = await self.repository.get(
                tenant_id,
                project_id,
                request.path_params["datasource_id"],
            )
        except QueryConfigurationError as exc:
            return _error(400, "invalid_datasource_request", str(exc))
        except LookupError:
            return _error(404, "resource_not_found", "Resource not found.")
        except DatasourceStoreUnavailable:
            return _error(
                503,
                "datasource_store_unavailable",
                "Datasource configuration is temporarily unavailable.",
            )
        if datasource is None:
            return _error(404, "resource_not_found", "Resource not found.")
        return JSONResponse(
            datasource.to_dict(
                include_role=not self._restricted(principal)
            )
        )

    async def create_datasource(
        self,
        request: Request,
    ) -> JSONResponse:
        try:
            tenant_id, principal = self._scope(request)
            request_id = _request_id(request)
            body = await _json_object(request)
            project_id = await self._require_project(
                tenant_id,
                body.pop("project_id", None),
            )
            datasource_id = body.pop("datasource_id", None)
            datasource = AthenaDatasource.from_mapping(
                body,
                tenant_id=tenant_id,
                project_id=project_id,
                datasource_id=datasource_id,
            )
            if not self.bindings.allows(
                tenant_id,
                project_id,
                datasource.role_arn,
            ):
                return _error(
                    403,
                    "role_binding_not_approved",
                    "The IAM role is not approved for this tenant project.",
                )
            await self._audit_mutation(
                event_type=AuditEventType.DATASOURCE_MUTATION_REQUEST,
                principal=principal,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                data={
                    "operation": "create",
                    "datasource_id": datasource.datasource_id,
                    "expected_revision": 0,
                    "changes": _redacted_changes(None, datasource),
                },
            )
            saved = await self.repository.save(
                datasource,
                expected_revision=0,
            )
            await self._audit_mutation(
                event_type=AuditEventType.DATASOURCE_MUTATION_RESULT,
                principal=principal,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                data={
                    "operation": "create",
                    "datasource_id": saved.datasource_id,
                    "status": "succeeded",
                    "revision": saved.revision,
                },
            )
        except QueryConfigurationError as exc:
            return _error(400, "invalid_datasource_request", str(exc))
        except DatasourceAuditUnavailable as exc:
            return _error(503, "datasource_audit_unavailable", str(exc))
        except LookupError:
            return _error(404, "resource_not_found", "Resource not found.")
        except DatasourceConflictError:
            return _error(
                409,
                "datasource_conflict",
                "Datasource already exists or changed concurrently.",
            )
        except DatasourceQuotaExceededError:
            return _error(
                409,
                "datasource_quota_exceeded",
                "The tenant datasource quota has been reached.",
            )
        except DatasourceStoreUnavailable:
            return _error(
                503,
                "datasource_store_unavailable",
                "Datasource configuration is temporarily unavailable.",
            )
        return JSONResponse(saved.to_dict(), status_code=201)

    async def update_datasource(
        self,
        request: Request,
    ) -> JSONResponse:
        try:
            tenant_id, principal = self._scope(request)
            request_id = _request_id(request)
            body = await _json_object(request)
            project_id = await self._require_project(
                tenant_id,
                body.pop("project_id", None),
            )
            expected_revision = _positive_revision(
                body.pop("expected_revision", None)
            )
            datasource_id = request.path_params["datasource_id"]
            if "datasource_id" in body:
                raise QueryConfigurationError(
                    "datasource_id is path-owned for updates"
                )
            current = await self.repository.get(
                tenant_id,
                project_id,
                datasource_id,
            )
            if current is None:
                return _error(
                    404,
                    "resource_not_found",
                    "Resource not found.",
                )
            candidate = AthenaDatasource.from_mapping(
                body,
                tenant_id=tenant_id,
                project_id=project_id,
                datasource_id=datasource_id,
                revision=expected_revision,
                created_at=current.created_at,
            )
            if not self.bindings.allows(
                tenant_id,
                project_id,
                candidate.role_arn,
            ):
                return _error(
                    403,
                    "role_binding_not_approved",
                    "The IAM role is not approved for this tenant project.",
                )
            await self._audit_mutation(
                event_type=AuditEventType.DATASOURCE_MUTATION_REQUEST,
                principal=principal,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                data={
                    "operation": "update",
                    "datasource_id": datasource_id,
                    "expected_revision": expected_revision,
                    "changes": _redacted_changes(current, candidate),
                },
            )
            saved = await self.repository.save(
                replace(
                    candidate,
                    created_at=current.created_at,
                ),
                expected_revision=expected_revision,
            )
            await self._audit_mutation(
                event_type=AuditEventType.DATASOURCE_MUTATION_RESULT,
                principal=principal,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                data={
                    "operation": "update",
                    "datasource_id": datasource_id,
                    "status": "succeeded",
                    "previous_revision": expected_revision,
                    "revision": saved.revision,
                },
            )
        except QueryConfigurationError as exc:
            return _error(400, "invalid_datasource_request", str(exc))
        except DatasourceAuditUnavailable as exc:
            return _error(503, "datasource_audit_unavailable", str(exc))
        except LookupError:
            return _error(404, "resource_not_found", "Resource not found.")
        except DatasourceConflictError:
            return _error(
                409,
                "datasource_conflict",
                "Datasource changed concurrently.",
            )
        except DatasourceStoreUnavailable:
            return _error(
                503,
                "datasource_store_unavailable",
                "Datasource configuration is temporarily unavailable.",
            )
        return JSONResponse(saved.to_dict())

    async def delete_datasource(
        self,
        request: Request,
    ) -> JSONResponse:
        try:
            tenant_id, principal = self._scope(request)
            request_id = _request_id(request)
            project_id = await self._require_project(
                tenant_id,
                request.query_params.get("project_id"),
            )
            raw_revision = request.query_params.get("expected_revision")
            try:
                parsed_revision = int(raw_revision or "")
            except ValueError as exc:
                raise QueryConfigurationError(
                    "expected_revision must be a positive integer"
                ) from exc
            expected_revision = _positive_revision(parsed_revision)
            datasource_id = request.path_params["datasource_id"]
            current = await self.repository.get(
                tenant_id,
                project_id,
                datasource_id,
            )
            if current is None:
                return _error(
                    404,
                    "resource_not_found",
                    "Resource not found.",
                )
            await self._audit_mutation(
                event_type=AuditEventType.DATASOURCE_MUTATION_REQUEST,
                principal=principal,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                data={
                    "operation": "delete",
                    "datasource_id": datasource_id,
                    "expected_revision": expected_revision,
                    "changes": _redacted_changes(current, None),
                },
            )
            await self.repository.delete(
                tenant_id,
                project_id,
                datasource_id,
                expected_revision=expected_revision,
            )
            await self._audit_mutation(
                event_type=AuditEventType.DATASOURCE_MUTATION_RESULT,
                principal=principal,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                data={
                    "operation": "delete",
                    "datasource_id": datasource_id,
                    "status": "succeeded",
                    "previous_revision": expected_revision,
                },
            )
        except QueryConfigurationError as exc:
            return _error(400, "invalid_datasource_request", str(exc))
        except DatasourceAuditUnavailable as exc:
            return _error(503, "datasource_audit_unavailable", str(exc))
        except LookupError:
            return _error(404, "resource_not_found", "Resource not found.")
        except DatasourceConflictError:
            return _error(
                409,
                "datasource_conflict",
                "Datasource changed concurrently or no longer exists.",
            )
        except DatasourceStoreUnavailable:
            return _error(
                503,
                "datasource_store_unavailable",
                "Datasource configuration is temporarily unavailable.",
            )
        return JSONResponse(
            {
                "status": "deleted",
                "datasource_id": datasource_id,
                "project_id": project_id,
            }
        )


def create_datasource_routes(api: DatasourceAPI) -> list[Route]:
    return [
        Route(
            "/admin/datasources",
            api.list_datasources,
            methods=["GET"],
        ),
        Route(
            "/admin/datasources",
            api.create_datasource,
            methods=["POST"],
        ),
        Route(
            "/admin/datasources/{datasource_id}",
            api.get_datasource,
            methods=["GET"],
        ),
        Route(
            "/admin/datasources/{datasource_id}",
            api.update_datasource,
            methods=["PUT"],
        ),
        Route(
            "/admin/datasources/{datasource_id}",
            api.delete_datasource,
            methods=["DELETE"],
        ),
    ]
