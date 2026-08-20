"""Admin API routes for API key management.

These four routes are the gateway's credential factory, which makes them the one
place where admin RBAC's resource-level granularity is not granular enough.
`AdminRBACMiddleware` authorizes on the first path segment, so `admin:projects`
grants *everything* under `/admin/projects/*` — including `POST
/admin/projects/{id}/keys`, and `admin:keys` grants `POST
/admin/keys/{key_id}/rotate`. Without the checks below, either scope is a full
privilege escalation: issue yourself a key with `scopes=['admin:*']`, or rotate
somebody else's `admin:*` key and read the replacement's raw value out of the
response. Both were reachable and confirmed; see
`tests/unit/test_admin_key_privilege_escalation.py`.

In legacy mode, a caller cannot mint authority it does not already hold or
operate on a credential outside its own project. In canonical mode, tenant
administrators manage keys across their tenant, viewers may inspect key
metadata, and legacy ``admin:`` scopes are rejected. Those checks live in the
handlers because the middleware sees only a path, while the handler knows the
requested scopes and the tenant-qualified target key.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.middleware.admin_rbac import scope_implies
from src.gateway.models import RequestContext
from src.gateway.security.audit_trail import (
    LEGACY_TENANT_ID,
    AuditEventType,
    AuditStoreUnavailable,
    AuditTrail,
)

if TYPE_CHECKING:
    from src.gateway.auth.api_key_service import APIKeyService

logger = logging.getLogger(__name__)

_CANONICAL_ROLES = frozenset(
    {
        "tenant_admin",
        "tenant_member",
        "tenant_auditor",
        "platform_admin",
    }
)
_CANONICAL_KEY_ADMINS = frozenset({"tenant_admin", "platform_admin"})
_CANONICAL_KEY_READERS = _CANONICAL_ROLES


class _TenantScopeError(ValueError):
    pass


def _caller(request: Request) -> RequestContext | None:
    """The authenticated identity behind this request, if any."""
    state = getattr(request, "state", None)
    return getattr(state, "context", None)


def _context_roles(ctx: RequestContext | None) -> set[str]:
    raw_roles = getattr(ctx, "roles", None)
    if not isinstance(raw_roles, (list, tuple, set, frozenset)):
        return set()
    return {role for role in raw_roles if isinstance(role, str)}


def _context_scopes(ctx: RequestContext | None) -> list[str]:
    raw_scopes = getattr(ctx, "scopes", None)
    if not isinstance(raw_scopes, (list, tuple, set, frozenset)):
        return []
    return [scope for scope in raw_scopes if isinstance(scope, str)]


def _is_canonical_context(ctx: RequestContext | None) -> bool:
    if ctx is None:
        return False
    return (
        getattr(ctx, "principal_id", None) is not None
        or bool(_context_roles(ctx) & _CANONICAL_ROLES)
    )


def _is_tenant_key_admin(ctx: RequestContext | None) -> bool:
    return _is_canonical_context(ctx) and bool(
        _context_roles(ctx) & _CANONICAL_KEY_ADMINS
    )


def _can_read_tenant_keys(ctx: RequestContext | None) -> bool:
    return _is_canonical_context(ctx) and bool(
        _context_roles(ctx) & _CANONICAL_KEY_READERS
    )


def _tenant_id(ctx: RequestContext | None) -> str | None:
    value = getattr(ctx, "tenant_id", None)
    if value is not None and (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
    ):
        raise _TenantScopeError("tenant_id must be a non-empty string")
    if value is not None:
        return value
    if ctx is not None and (
        getattr(ctx, "principal_id", None) is not None
        or _context_roles(ctx) & _CANONICAL_ROLES
    ):
        raise _TenantScopeError(
            "authenticated tenant context is missing tenant_id"
        )
    return None


def _actor(ctx: RequestContext | None, legacy_actor: object) -> str:
    if not _is_canonical_context(ctx):
        if (
            isinstance(legacy_actor, str)
            and legacy_actor.strip()
            and legacy_actor == legacy_actor.strip()
        ):
            return legacy_actor
        raise _TenantScopeError("actor attribution must be a non-empty string")
    for attribute in ("principal_id", "user_id"):
        value = getattr(ctx, attribute, None)
        if (
            isinstance(value, str)
            and value.strip()
            and value == value.strip()
        ):
            return value
    raise _TenantScopeError(
        "authenticated tenant context is missing principal attribution"
    )


def _scope_error_response(exc: _TenantScopeError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "type": "invalid_tenant_scope",
                "message": str(exc),
            }
        },
    )


def _store_scope_error_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "type": "key_store_unavailable",
                "message": "Tenant API-key data is temporarily unavailable.",
            }
        },
    )


def _audit_error_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "type": "audit_store_unavailable",
                "message": "API-key lifecycle auditing is temporarily unavailable.",
            }
        },
    )


def _request_id(request: Request) -> str:
    state = getattr(request, "state", None)
    state_request_id = getattr(state, "request_id", None)
    headers = getattr(request, "headers", None)
    header_request_id = (
        headers.get("x-request-id")
        if headers is not None
        else None
    )
    for value in (state_request_id, header_request_id):
        if isinstance(value, str) and value.strip():
            return value.strip()
    request_id = f"key_{uuid.uuid4().hex}"
    if state is not None:
        state.request_id = request_id
    return request_id


def _is_superadmin(ctx: RequestContext | None) -> bool:
    """Whether the caller holds unrestricted admin authority.

    Only these callers may grant arbitrary scopes or reach across projects. A
    holder of a narrow scope like ``admin:projects`` is deliberately not one:
    that scope reaches this route only because RBAC matches on the path's first
    segment, not because it was meant to confer key-minting power.
    """
    if ctx is None:
        return False
    if _is_canonical_context(ctx):
        return False
    return "admin" in _context_roles(ctx) or "admin:*" in _context_scopes(ctx)


def _may_grant(ctx: RequestContext | None, requested: list[str]) -> str | None:
    """Return a refusal reason if the caller cannot grant ``requested``.

    A caller may grant an admin scope only if something it holds already implies
    it — so ``admin:projects`` can hand out the narrower ``admin:projects:read``
    but not ``admin:quotas`` or ``admin:*``. Non-admin scopes (``chat`` and
    friends) are freely grantable: the concern is escalation of *admin* authority,
    not handing out ordinary gateway access.
    """
    if _is_canonical_context(ctx):
        legacy_admin_scopes = sorted(
            scope for scope in requested if scope.startswith("admin:")
        )
        if legacy_admin_scopes:
            return (
                "Canonical API keys cannot carry legacy admin scopes: "
                + ", ".join(legacy_admin_scopes)
            )
        return None
    if _is_superadmin(ctx):
        return None
    held = _context_scopes(ctx)
    escalating = [
        s
        for s in requested
        if s.startswith("admin:")
        and not any(scope_implies(h, s) for h in held)
    ]
    if escalating:
        return (
            "Cannot issue a key with scopes the caller does not hold: "
            + ", ".join(sorted(escalating))
        )
    return None


class KeyManagementAPI:
    """Handles CRUD operations for project-scoped API keys."""

    def __init__(
        self,
        api_key_service: APIKeyService,
        mode: str = "ENFORCE",
        audit_trail: AuditTrail | None = None,
    ) -> None:
        self.api_key_service = api_key_service
        self.mode = mode
        persistence = api_key_service.persistence
        self.audit_trail = (
            audit_trail
            or getattr(persistence, "_audit_trail", None)
            or AuditTrail(persistence=persistence)
        )

    async def _record_lifecycle_event(
        self,
        *,
        event_type: AuditEventType,
        actor: str,
        tenant_id: str | None,
        project_id: str,
        request_id: str,
        outcome: str,
        data: dict,
    ) -> JSONResponse | None:
        try:
            if (
                tenant_id is not None
                and self.api_key_service.persistence.enabled
                and not self.audit_trail.durable_enabled
            ):
                raise AuditStoreUnavailable(
                    "Durable key audit persistence is required"
                )
            await self.audit_trail.record(
                event_type=event_type,
                user_id=actor,
                project_id=project_id,
                request_id=request_id,
                tenant_id=tenant_id or LEGACY_TENANT_ID,
                data={
                    **data,
                    "actor_id": actor,
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "outcome": outcome,
                },
            )
        except Exception:
            logger.error(
                "Failed to record %s for tenant %s",
                event_type.value,
                tenant_id,
                exc_info=True,
            )
            return _audit_error_response()
        return None

    def _forbid(self, message: str, ctx: RequestContext | None) -> JSONResponse | None:
        """Deny with 403, or in LOG_ONLY just record the attempt.

        Mirrors `AdminRBACMiddleware`: LOG_ONLY is the local-development default,
        where there is no authenticated context at all, and failing closed would
        make the key routes unusable before an operator has any credential to use
        them with.
        """
        user = getattr(ctx, "user_id", "<no context>")
        if self.mode != "ENFORCE":
            logger.warning(
                "Would deny key operation for '%s' (LOG_ONLY): %s", user, message
            )
            return None
        logger.warning("Denied key operation for '%s': %s", user, message)
        return JSONResponse(
            status_code=403,
            content={"error": {"type": "authorization_error", "message": message}},
        )

    async def issue_key(self, request: Request) -> JSONResponse:
        """POST /admin/projects/{id}/keys"""
        project_id = request.path_params["id"]
        body = await request.json()

        name = body.get("name", "Unnamed key")
        scopes = body.get("scopes", ["chat:invoke"])
        expires_at_str = body.get("expires_at")

        ctx = _caller(request)
        request_id = _request_id(request)
        try:
            tenant_id = _tenant_id(ctx)
            created_by = _actor(
                ctx,
                body.get("created_by", "admin"),
            )
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        context_project_id = getattr(ctx, "project_id", None)
        if (
            not _is_superadmin(ctx)
            and not _is_tenant_key_admin(ctx)
            and ctx is not None
            and context_project_id != project_id
        ):
            denial = self._forbid(
                f"Caller scoped to project '{context_project_id}' cannot issue keys "
                f"for project '{project_id}'",
                ctx,
            )
            if denial is not None:
                audit_error = await self._record_lifecycle_event(
                    event_type=AuditEventType.KEY_ISSUED,
                    actor=created_by,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    request_id=request_id,
                    outcome="denied",
                    data={"key_id": None},
                )
                return audit_error or denial
        reason = _may_grant(ctx, list(scopes))
        if reason:
            denial = self._forbid(reason, ctx)
            if denial is not None:
                audit_error = await self._record_lifecycle_event(
                    event_type=AuditEventType.KEY_ISSUED,
                    actor=created_by,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    request_id=request_id,
                    outcome="denied",
                    data={"key_id": None},
                )
                return audit_error or denial

        expires_at = None
        if expires_at_str:
            expires_at = datetime.fromisoformat(expires_at_str)

        try:
            key_record, raw_key = await self.api_key_service.issue_key(
                project_id=project_id,
                name=name,
                scopes=scopes,
                created_by=created_by,
                expires_at=expires_at,
                tenant_id=tenant_id,
            )
        except Exception:
            audit_error = await self._record_lifecycle_event(
                event_type=AuditEventType.KEY_ISSUED,
                actor=created_by,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                outcome="failed",
                data={"key_id": None},
            )
            return audit_error or _store_scope_error_response()
        if getattr(key_record, "tenant_id", None) != tenant_id:
            logger.error(
                "API key service returned a cross-tenant record for key %s",
                getattr(key_record, "key_id", "<unknown>"),
            )
            return _store_scope_error_response()
        audit_error = await self._record_lifecycle_event(
            event_type=AuditEventType.KEY_ISSUED,
            actor=created_by,
            tenant_id=tenant_id,
            project_id=project_id,
            request_id=request_id,
            outcome="success",
            data={
                "key_id": key_record.key_id,
                "scopes": list(key_record.scopes),
            },
        )
        if audit_error is not None:
            try:
                await self.api_key_service.revoke_key(
                    key_record.key_id,
                    tenant_id,
                    revoked_by=created_by,
                )
            except Exception:
                logger.critical(
                    "Failed to contain unaudited issued API key %s",
                    key_record.key_id,
                    exc_info=True,
                )
            return audit_error

        return JSONResponse(
            status_code=201,
            content={
                "key_id": key_record.key_id,
                "key": raw_key,
                "project_id": key_record.project_id,
                "name": key_record.name,
                "scopes": key_record.scopes,
                "created_at": key_record.created_at.isoformat(),
                "expires_at": key_record.expires_at.isoformat() if key_record.expires_at else None,
                "warning": "Store this key securely — it will not be shown again.",
            },
        )

    async def list_keys(self, request: Request) -> JSONResponse:
        """GET /admin/projects/{id}/keys"""
        project_id = request.path_params["id"]

        ctx = _caller(request)
        request_id = _request_id(request)
        try:
            tenant_id = _tenant_id(ctx)
            actor = _actor(ctx, "admin")
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        context_project_id = getattr(ctx, "project_id", None)
        if (
            not _is_superadmin(ctx)
            and not _can_read_tenant_keys(ctx)
            and ctx is not None
            and context_project_id != project_id
        ):
            denial = self._forbid(
                f"Caller scoped to project '{context_project_id}' cannot list keys "
                f"for project '{project_id}'",
                ctx,
            )
            if denial is not None:
                audit_error = await self._record_lifecycle_event(
                    event_type=AuditEventType.KEY_LISTED,
                    actor=actor,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    request_id=request_id,
                    outcome="denied",
                    data={"key_ids": []},
                )
                return audit_error or denial

        try:
            keys = await self.api_key_service.list_keys(
                project_id,
                tenant_id,
            )
        except Exception:
            audit_error = await self._record_lifecycle_event(
                event_type=AuditEventType.KEY_LISTED,
                actor=actor,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                outcome="failed",
                data={"key_ids": []},
            )
            return audit_error or _store_scope_error_response()
        if any(getattr(key, "tenant_id", None) != tenant_id for key in keys):
            logger.error(
                "API key service returned cross-tenant records for project %s",
                project_id,
            )
            return _store_scope_error_response()
        audit_error = await self._record_lifecycle_event(
            event_type=AuditEventType.KEY_LISTED,
            actor=actor,
            tenant_id=tenant_id,
            project_id=project_id,
            request_id=request_id,
            outcome="success",
            data={
                "key_ids": [key.key_id for key in keys],
                "key_count": len(keys),
            },
        )
        if audit_error is not None:
            return audit_error

        return JSONResponse(
            content=[
                {
                    "key_id": k.key_id,
                    "name": k.name,
                    "project_id": k.project_id,
                    "scopes": k.scopes,
                    "created_by": k.created_by,
                    "created_at": k.created_at.isoformat(),
                    "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                    "revoked": k.revoked,
                    "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
                    "revoked_by": k.revoked_by,
                    "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
                }
                for k in keys
            ]
        )

    async def _owns_key(
        self,
        ctx: RequestContext | None,
        key_id: str,
        tenant_id: str | None,
    ) -> bool:
        """Whether ``key_id`` belongs to the caller's own project.

        Asked via the public ``list_keys`` rather than a lookup by id, so this
        cannot become a way to confirm the existence of keys in other projects.
        """
        if ctx is None:
            return False
        project_id = getattr(ctx, "project_id", None)
        if not isinstance(project_id, str) or not project_id:
            return False
        keys = await self.api_key_service.list_keys(
            project_id,
            tenant_id,
        )
        return any(
            key.key_id == key_id
            and getattr(key, "tenant_id", None) == tenant_id
            for key in keys
        )

    async def revoke_key(self, request: Request) -> JSONResponse:
        """DELETE /admin/keys/{key_id}"""
        key_id = request.path_params["key_id"]

        ctx = _caller(request)
        request_id = _request_id(request)
        try:
            tenant_id = _tenant_id(ctx)
            revoked_by = _actor(ctx, "admin")
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        if (
            not _is_superadmin(ctx)
            and not _is_tenant_key_admin(ctx)
            and not await self._owns_key(
            ctx,
            key_id,
            tenant_id,
            )
        ):
            denial = self._forbid(
                f"Caller cannot revoke key '{key_id}' outside its own project", ctx
            )
            if denial is not None:
                audit_error = await self._record_lifecycle_event(
                    event_type=AuditEventType.KEY_REVOKED,
                    actor=revoked_by,
                    tenant_id=tenant_id,
                    project_id=getattr(ctx, "project_id", "unknown"),
                    request_id=request_id,
                    outcome="denied",
                    data={"key_id": key_id},
                )
                return audit_error or denial

        try:
            target = await self.api_key_service.get_key(key_id, tenant_id)
            project_id = (
                target.project_id
                if target is not None
                else getattr(ctx, "project_id", "unknown")
            )
            success = await self.api_key_service.revoke_key(
                key_id,
                tenant_id,
                revoked_by=revoked_by,
            )
        except Exception:
            project_id = getattr(ctx, "project_id", "unknown")
            audit_error = await self._record_lifecycle_event(
                event_type=AuditEventType.KEY_REVOKED,
                actor=revoked_by,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                outcome="failed",
                data={"key_id": key_id},
            )
            return audit_error or _store_scope_error_response()

        if not success:
            audit_error = await self._record_lifecycle_event(
                event_type=AuditEventType.KEY_REVOKED,
                actor=revoked_by,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                outcome="not_found_or_inactive",
                data={"key_id": key_id},
            )
            if audit_error is not None:
                return audit_error
            return JSONResponse(
                status_code=404,
                content={"error": f"Key '{key_id}' not found."},
            )
        audit_error = await self._record_lifecycle_event(
            event_type=AuditEventType.KEY_REVOKED,
            actor=revoked_by,
            tenant_id=tenant_id,
            project_id=project_id,
            request_id=request_id,
            outcome="success",
            data={"key_id": key_id},
        )
        if audit_error is not None:
            return audit_error

        return JSONResponse(content={"status": "revoked", "key_id": key_id})

    async def rotate_key(self, request: Request) -> JSONResponse:
        """POST /admin/keys/{key_id}/rotate

        Rotation is an escalation primitive, not just a lifecycle operation: it
        returns the replacement's raw value, and ``APIKeyService.rotate_key``
        copies the *old* key's scopes onto it. So rotating a key you don't own is
        equivalent to being handed that key. An ownership check alone is not
        enough — the confirmed attack was entirely inside one project, where an
        ``admin:keys`` holder rotated a colleague's ``admin:*`` key and used the
        response. The caller must therefore also hold every admin scope the
        target carries.
        """
        key_id = request.path_params["key_id"]
        body = await request.json() if await request.body() else {}

        ctx = _caller(request)
        request_id = _request_id(request)
        try:
            tenant_id = _tenant_id(ctx)
            rotated_by = _actor(
                ctx,
                body.get("rotated_by", "admin"),
            )
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        if not _is_superadmin(ctx) and not _is_tenant_key_admin(ctx):
            if not await self._owns_key(ctx, key_id, tenant_id):
                denial = self._forbid(
                    f"Caller cannot rotate key '{key_id}' outside its own project", ctx
                )
                if denial is not None:
                    audit_error = await self._record_lifecycle_event(
                        event_type=AuditEventType.KEY_ROTATED,
                        actor=rotated_by,
                        tenant_id=tenant_id,
                        project_id=getattr(ctx, "project_id", "unknown"),
                        request_id=request_id,
                        outcome="denied",
                        data={
                            "old_key_id": key_id,
                            "new_key_id": None,
                        },
                    )
                    return audit_error or denial
            else:
                target = next(
                    (
                        k
                        for k in await self.api_key_service.list_keys(
                            getattr(ctx, "project_id", ""),
                            tenant_id,
                        )
                        if (
                            k.key_id == key_id
                            and getattr(k, "tenant_id", None) == tenant_id
                        )
                    ),
                    None,
                )
                reason = _may_grant(ctx, list(target.scopes) if target else [])
                if reason:
                    denial = self._forbid(
                        f"Cannot rotate key '{key_id}': it carries admin scopes the "
                        "caller does not hold, and rotation would return its "
                        "replacement's raw value",
                        ctx,
                    )
                    if denial is not None:
                        audit_error = await self._record_lifecycle_event(
                            event_type=AuditEventType.KEY_ROTATED,
                            actor=rotated_by,
                            tenant_id=tenant_id,
                            project_id=getattr(
                                ctx,
                                "project_id",
                                "unknown",
                            ),
                            request_id=request_id,
                            outcome="denied",
                            data={
                                "old_key_id": key_id,
                                "new_key_id": None,
                            },
                        )
                        return audit_error or denial

        try:
            target = await self.api_key_service.get_key(key_id, tenant_id)
            project_id = (
                target.project_id
                if target is not None
                else getattr(ctx, "project_id", "unknown")
            )
            result = await self.api_key_service.rotate_key(
                key_id,
                rotated_by,
                tenant_id,
            )
        except Exception:
            project_id = getattr(ctx, "project_id", "unknown")
            audit_error = await self._record_lifecycle_event(
                event_type=AuditEventType.KEY_ROTATED,
                actor=rotated_by,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                outcome="failed",
                data={
                    "old_key_id": key_id,
                    "new_key_id": None,
                },
            )
            return audit_error or _store_scope_error_response()
        if result is None:
            audit_error = await self._record_lifecycle_event(
                event_type=AuditEventType.KEY_ROTATED,
                actor=rotated_by,
                tenant_id=tenant_id,
                project_id=project_id,
                request_id=request_id,
                outcome="not_found_or_inactive",
                data={
                    "old_key_id": key_id,
                    "new_key_id": None,
                },
            )
            if audit_error is not None:
                return audit_error
            return JSONResponse(
                status_code=404,
                content={"error": f"Key '{key_id}' not found."},
            )

        new_key, raw_key = result
        if getattr(new_key, "tenant_id", None) != tenant_id:
            logger.error(
                "API key service returned a cross-tenant rotation for key %s",
                key_id,
            )
            return _store_scope_error_response()
        audit_error = await self._record_lifecycle_event(
            event_type=AuditEventType.KEY_ROTATED,
            actor=rotated_by,
            tenant_id=tenant_id,
            project_id=new_key.project_id,
            request_id=request_id,
            outcome="success",
            data={
                "old_key_id": key_id,
                "new_key_id": new_key.key_id,
            },
        )
        if audit_error is not None:
            try:
                await self.api_key_service.revoke_key(
                    new_key.key_id,
                    tenant_id,
                    revoked_by=rotated_by,
                )
            except Exception:
                logger.critical(
                    "Failed to contain unaudited rotated API key %s",
                    new_key.key_id,
                    exc_info=True,
                )
            return audit_error
        return JSONResponse(
            status_code=201,
            content={
                "old_key_id": key_id,
                "old_key_status": "revoked",
                "new_key_id": new_key.key_id,
                "key": raw_key,
                "project_id": new_key.project_id,
                "name": new_key.name,
                "scopes": new_key.scopes,
                "warning": "Store this key securely — it will not be shown again.",
            },
        )


def create_key_routes(key_api: KeyManagementAPI) -> list[Route]:
    """Create Starlette routes for API key management."""
    return [
        Route("/admin/projects/{id}/keys", key_api.issue_key, methods=["POST"]),
        Route("/admin/projects/{id}/keys", key_api.list_keys, methods=["GET"]),
        Route("/admin/keys/{key_id}", key_api.revoke_key, methods=["DELETE"]),
        Route("/admin/keys/{key_id}/rotate", key_api.rotate_key, methods=["POST"]),
    ]
