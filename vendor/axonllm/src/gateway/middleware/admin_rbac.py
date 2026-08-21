"""Admin RBAC middleware — restricts /admin/* endpoints to authorized users.

Canonical tenant roles are the primary policy:
- ``tenant_admin`` may read and write tenant configuration.
- ``tenant_member`` and ``tenant_auditor`` may read it, but never mutate it.
- ``service`` has no control-plane access.

Legacy ``admin`` roles and ``admin:*`` scopes remain supported during migration.

Scopes name a resource and, optionally, an access level:

    admin:*                 everything
    admin:*:read            read every resource, write none
    admin:quotas            read and write /admin/quotas/*   (no suffix = both)
    admin:quotas:read       read /admin/quotas/* only
    admin:quotas:write      read and write /admin/quotas/*

A bare ``admin:<resource>`` grants both, so scopes issued before ``:read`` existed
keep the access they had — the suffix narrows, it never silently widens or
downgrades. ``:write`` implies read: an operator who can reset a quota can
already see the value they are resetting, and splitting them would only produce
keys that mutate blind.

**Read and write are classified by effect, not by HTTP method.** Four admin
POSTs are named like inspections but mutate state, and are treated as writes:
``/admin/quotas/simulate`` consumes the project's rate-limit budget,
``/admin/regions/health/check`` updates spoke status (and so changes routing),
``/admin/regions/route`` exercises the live router, and
``/admin/webhooks/{name}/test`` sends a real HTTP request to an external
endpoint. ``POST /admin/pii/preview`` is the one POST that genuinely persists
nothing, so a ``:read`` scope reaches it. Classifying those four by method would
hand a nominally read-only credential the ability to exhaust a rate limit or ping
an outside host.

In LOG_ONLY mode (default), denials are logged but not enforced.
In ENFORCE mode, returns 403.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.gateway.auth.authorization import Action, ResourceRef, authorize
from src.gateway.models import Principal, TenantRole

if TYPE_CHECKING:
    from src.gateway.security.audit_trail import AuditTrail

logger = logging.getLogger(__name__)

TENANT_ADMIN_ROLE = "tenant_admin"
TENANT_READ_ROLES = frozenset({"tenant_member", "tenant_auditor"})
PLATFORM_ADMIN_ROLE = "platform_admin"
BREAK_GLASS_TARGET_HEADER = "x-axon-target-tenant"
_TENANT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
CANONICAL_CONTROL_PLANE_ROLES = frozenset({
    TENANT_ADMIN_ROLE,
    *TENANT_READ_ROLES,
    PLATFORM_ADMIN_ROLE,
})
PLATFORM_RESOURCES = frozenset({
    "architecture",
    "catalog",
    "catalog-drift",
    "health",
    "models",
    "pricing-drift",
    "production-checklist",
    "regions",
})
TENANT_RESOURCE_ACTIONS = {
    "audit": (Action.AUDIT_READ, Action.AUDIT_EXPORT),
    "keys": (Action.API_KEY_READ, Action.API_KEY_MANAGE),
    "policies": (Action.POLICY_READ, Action.POLICY_WRITE),
    "quotas": (Action.QUOTA_READ, Action.QUOTA_WRITE),
    "usage": (Action.USAGE_READ, Action.USAGE_EXPORT),
    "webhooks": (Action.WEBHOOK_READ, Action.WEBHOOK_WRITE),
}

READ_ONLY_WRITE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""Methods that are reads by default, before the by-effect overrides below."""

WRITE_EFFECT_PATHS = frozenset({
    "/admin/quotas/simulate",
    "/admin/regions/health/check",
    "/admin/regions/route",
})
"""Non-GET paths that a ``:read`` scope must not reach, despite reading like
inspections. Each mutates: consumes rate-limit budget, updates spoke status,
or exercises the live router. ``/admin/webhooks/{name}/test`` is matched
separately since it carries a path parameter."""

READ_EFFECT_PATHS = frozenset({
    "/admin/pii/preview",
})
"""Non-GET paths that ``:read`` may reach because they persist nothing. Kept as
an explicit allowlist rather than a naming convention — ``preview``, ``test``
and ``simulate`` are used by both kinds of route here, so the name is not
evidence."""


def classify_access(method: str, path: str) -> str:
    """Return ``"read"`` or ``"write"`` for a request, by effect.

    Method is the default signal; the two path sets above override it where the
    method lies about what the handler does.
    """
    if path in WRITE_EFFECT_PATHS:
        return "write"
    if path.startswith("/admin/webhooks/") and path.endswith("/test"):
        return "write"  # fires a real HTTP request at an external endpoint
    if path in READ_EFFECT_PATHS:
        return "read"
    return "read" if method.upper() in READ_ONLY_WRITE_METHODS else "write"


def parse_admin_scope(scope: str) -> tuple[str, str]:
    """Split ``admin:<resource>[:<access>]`` into ``(resource, access)``.

    A missing suffix yields ``"write"``, which is what keeps pre-existing
    ``admin:quotas`` keys working exactly as they did. An unrecognised suffix is
    treated as part of the resource name rather than as an access level, so a typo
    like ``admin:quotas:raed`` fails closed (it matches no resource) instead of
    quietly granting write.
    """
    body = scope[len("admin:"):] if scope.startswith("admin:") else scope
    resource, sep, suffix = body.rpartition(":")
    if sep and suffix in ("read", "write"):
        return resource, suffix
    return body, "write"


def scope_implies(held: str, requested: str) -> bool:
    """Whether holding ``held`` confers everything ``requested`` grants.

    Used by the key-issuance guard so a caller can delegate a *narrower* slice of
    what it holds: ``admin:projects`` may grant ``admin:projects:read``, and
    ``admin:*`` may grant anything. Without this, exact string comparison would
    refuse to hand out a subset of one's own authority — the one delegation that
    is unambiguously safe.
    """
    if held == requested:
        return True
    if not held.startswith("admin:") or not requested.startswith("admin:"):
        return False
    held_resource, held_access = parse_admin_scope(held)
    req_resource, req_access = parse_admin_scope(requested)
    if held_resource != "*" and held_resource != req_resource:
        return False
    return held_access == "write" or held_access == req_access


class AdminRBACMiddleware(BaseHTTPMiddleware):
    """Enforces role/scope requirements on admin endpoints."""

    def __init__(
        self,
        app,
        mode: str = "ENFORCE",
        audit_trail: AuditTrail | None = None,
    ):
        super().__init__(app)
        self.mode = mode
        self._audit_trail = audit_trail

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if not path.startswith("/admin/"):
            return await call_next(request)

        # Static assets and dashboard page are public
        if path.startswith("/admin/static") or path == "/admin/dashboard":
            return await call_next(request)

        ctx = getattr(request.state, "context", None)
        if ctx is None:
            if self.mode == "ENFORCE":
                return self._deny("No authentication context")
            return await call_next(request)

        access = classify_access(request.method, path)
        principal = getattr(request.state, "principal", None)

        if self._is_break_glass_attempt(ctx, path, principal):
            try:
                target_tenant_id = self._target_tenant_id(request)
                reason = self._break_glass_reason(request)
            except ValueError as exc:
                return self._invalid_break_glass_request(str(exc))

            if not self._is_canonical_platform_context(ctx, principal):
                return self._deny(
                    "Break-glass access requires a server-resolved "
                    "platform administrator."
                )

            action = self._tenant_action(path, access)
            decision = authorize(
                principal,
                action,
                ResourceRef(
                    resource_type=self._extract_resource(path) or "admin",
                    resource_id=path,
                    tenant_id=target_tenant_id,
                ),
                break_glass_reason=reason,
            )
            try:
                await self._record_break_glass(
                    ctx=ctx,
                    target_tenant_id=target_tenant_id,
                    path=path,
                    method=request.method,
                    access=access,
                    reason=reason,
                    allowed=decision.allowed,
                )
            except Exception:
                logger.error(
                    "Break-glass audit append failed user=%s tenant=%s path=%s",
                    ctx.user_id,
                    target_tenant_id,
                    path,
                    exc_info=True,
                )
                return self._unavailable()

            if not decision.allowed:
                return self._deny(
                    "Platform tenant access requires a distinct target tenant "
                    "and a non-empty break-glass reason."
                )

            # The canonical principal remains attached with its home tenant.
            # Only the handler context is rebound, after authorization and
            # durable audit agree on the exact target.
            request.state.context = replace(
                ctx,
                tenant_id=target_tenant_id,
                project_id="",
            )
            request.state.break_glass_target_tenant_id = target_tenant_id
            return await call_next(request)

        if self._is_authorized(
            ctx,
            path,
            access,
            principal=principal,
        ):
            return await call_next(request)

        if self.mode == "ENFORCE":
            resource = self._extract_resource(path)
            return self._deny(
                f"User '{ctx.user_id}' lacks {access} access to '{resource}'. "
                f"Required: 'admin' role, 'admin:*', or "
                f"'admin:{resource}:{access}' scope."
            )

        logger.warning(
            "Admin RBAC DENY (LOG_ONLY) user=%s path=%s access=%s roles=%s scopes=%s",
            ctx.user_id, path, access, ctx.roles, ctx.scopes,
        )
        return await call_next(request)

    def _is_break_glass_attempt(
        self,
        ctx,
        path: str,
        principal: Principal | None = None,
    ) -> bool:
        roles = set(getattr(ctx, "roles", ()))
        principal_is_platform = (
            isinstance(principal, Principal)
            and TenantRole.PLATFORM_ADMIN in principal.roles
        )
        return (
            (PLATFORM_ADMIN_ROLE in roles or principal_is_platform)
            and self._extract_resource(path) not in PLATFORM_RESOURCES
        )

    @staticmethod
    def _is_canonical_platform_context(ctx, principal: object) -> bool:
        if not isinstance(principal, Principal):
            return False
        expected_roles = {role.value for role in principal.roles}
        return (
            TenantRole.PLATFORM_ADMIN in principal.roles
            and getattr(ctx, "principal_id", None) == principal.principal_id
            and getattr(ctx, "user_id", None) == principal.principal_id
            and getattr(ctx, "tenant_id", None) == principal.tenant_id
            and set(getattr(ctx, "roles", ())) == expected_roles
            and getattr(ctx, "auth_method", None) is principal.auth_method
            and getattr(ctx, "authorization_version", None)
            == principal.authorization_version
        )

    @staticmethod
    def _break_glass_reason(request: Request) -> str:
        values = request.headers.getlist("x-axon-break-glass-reason")
        if len(values) > 1:
            raise ValueError(
                "X-Axon-Break-Glass-Reason must be supplied at most once"
            )
        return values[0] if values else ""

    @staticmethod
    def _target_tenant_id(request: Request) -> str:
        values = request.headers.getlist(BREAK_GLASS_TARGET_HEADER)
        if len(values) != 1:
            raise ValueError(
                "Exactly one X-Axon-Target-Tenant header is required"
            )
        tenant_id = values[0]
        if (
            tenant_id != tenant_id.strip()
            or _TENANT_ID_PATTERN.fullmatch(tenant_id) is None
        ):
            raise ValueError(
                "X-Axon-Target-Tenant contains an invalid tenant identifier"
            )

        query_values = request.query_params.getlist("tenant_id")
        if len(query_values) > 1 or any(
            value != tenant_id for value in query_values
        ):
            raise ValueError(
                "tenant_id query parameters must not conflict with "
                "X-Axon-Target-Tenant"
            )
        return tenant_id

    def _tenant_action(self, path: str, access: str) -> Action:
        if "/members" in path:
            return (
                Action.MEMBERSHIP_READ
                if access == "read"
                else Action.MEMBERSHIP_WRITE
            )
        if "/keys" in path:
            return (
                Action.API_KEY_READ
                if access == "read"
                else Action.API_KEY_MANAGE
            )
        if path == "/admin/audit/export" or path.startswith(
            "/admin/audit/exports/"
        ):
            return Action.AUDIT_EXPORT
        if path == "/admin/usage/export" or path.startswith(
            "/admin/usage/exports/"
        ):
            return Action.USAGE_EXPORT
        resource = self._extract_resource(path)
        read_action, write_action = TENANT_RESOURCE_ACTIONS.get(
            resource,
            (Action.TENANT_CONFIG_READ, Action.TENANT_CONFIG_WRITE),
        )
        return read_action if access == "read" else write_action

    async def _record_break_glass(
        self,
        *,
        ctx,
        target_tenant_id: str,
        path: str,
        method: str,
        access: str,
        reason: str,
        allowed: bool,
    ) -> None:
        if self._audit_trail is None:
            raise RuntimeError("break-glass audit trail is not configured")
        principal_id = getattr(ctx, "principal_id", None)
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise RuntimeError("break-glass principal context is missing")
        await self._audit_trail.record_break_glass_access(
            user_id=ctx.user_id,
            principal_id=principal_id,
            tenant_id=target_tenant_id,
            project_id="",
            request_id=f"breakglass_{uuid.uuid4().hex}",
            route=path,
            method=method.upper(),
            reason=reason,
            result="allowed" if allowed else "denied",
            access=access,
        )

    def _is_authorized(
        self,
        ctx,
        path: str = "",
        access: str = "write",
        *,
        break_glass_reason: str | None = None,
        principal: Principal | None = None,
        target_tenant_id: str | None = None,
    ) -> bool:
        """Whether ``ctx`` may perform ``access`` on ``path``.

        ``access`` defaults to ``"write"`` so that a caller which forgets to pass
        it gets the stricter check rather than silently authorizing mutations.
        """
        roles = set(ctx.roles)
        resource = self._extract_resource(path)
        canonical = (
            getattr(ctx, "principal_id", None) is not None
            or not roles.isdisjoint(CANONICAL_CONTROL_PLANE_ROLES)
        )

        if canonical:
            if resource in PLATFORM_RESOURCES:
                if not isinstance(principal, Principal):
                    return False
                platform_action = (
                    Action.PLATFORM_READ
                    if access == "read"
                    else Action.PLATFORM_WRITE
                )
                return authorize(
                    principal,
                    platform_action,
                    ResourceRef(
                        resource_type=resource,
                        resource_id=path,
                        tenant_id=None,
                    ),
                ).allowed
            if PLATFORM_ADMIN_ROLE in roles:
                if (
                    not self._is_canonical_platform_context(ctx, principal)
                    or target_tenant_id is None
                ):
                    return False
                return authorize(
                    principal,
                    self._tenant_action(path, access),
                    ResourceRef(
                        resource_type=resource or "admin",
                        resource_id=path or "/admin",
                        tenant_id=target_tenant_id,
                    ),
                    break_glass_reason=break_glass_reason,
                ).allowed
            if TENANT_ADMIN_ROLE in roles:
                return True
            if not roles.isdisjoint(TENANT_READ_ROLES):
                return access == "read"
            # Canonical authority comes only from server-held roles. Legacy
            # admin scopes must not turn a service principal into a tenant
            # administrator.
            return False

        if "admin" in roles:
            return True
        for scope in ctx.scopes:
            if not scope.startswith("admin:"):
                continue
            scope_resource, scope_access = parse_admin_scope(scope)
            if scope_resource not in ("*", resource):
                continue
            if scope_access == "write" or scope_access == access:
                return True
        return False

    def _extract_resource(self, path: str) -> str:
        """Extract the admin resource from path, e.g. /admin/quotas/proj:x -> quotas."""
        parts = path.strip("/").split("/")
        if len(parts) >= 2:
            return parts[1]
        return ""

    def _deny(self, message: str) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={
                "error": {
                    "type": "authorization_error",
                    "message": message,
                    "code": "admin_access_denied",
                }
            },
        )

    @staticmethod
    def _unavailable() -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "type": "service_unavailable",
                    "message": "Break-glass authorization is unavailable.",
                    "code": "break_glass_audit_unavailable",
                }
            },
        )

    @staticmethod
    def _invalid_break_glass_request(message: str) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "type": "invalid_request",
                    "message": message,
                    "code": "invalid_break_glass_target",
                }
            },
        )
