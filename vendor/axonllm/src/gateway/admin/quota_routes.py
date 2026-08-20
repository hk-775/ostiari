"""Admin API routes for quota and usage control."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.auth.policy_hierarchy import PolicyHierarchyStoreUnavailable

if TYPE_CHECKING:
    from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver
    from src.gateway.cost_tracker import CostTracker
    from src.gateway.models import Project
    from src.gateway.quota_enforcer import QuotaEnforcer

_CANONICAL_ROLES = frozenset(
    {
        "tenant_admin",
        "tenant_member",
        "tenant_auditor",
        "platform_admin",
    }
)


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
    supplied_tenant_id: object,
) -> str | None:
    """Prefer authenticated tenant scope and reject cross-tenant overrides."""
    supplied = _normalize_tenant_id(supplied_tenant_id)
    context = _request_context(request)
    authenticated = _normalize_tenant_id(
        getattr(context, "tenant_id", None)
    )
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
    return supplied


def _authorized_project(
    request: Request,
    project_id: str,
    tenant_id: str | None,
) -> Project | None:
    context = _request_context(request)
    project = getattr(context, "authorized_project", None)
    if project is None or project.project_id != project_id:
        return None
    project_tenant_id = getattr(project, "tenant_id", None)
    if project_tenant_id != tenant_id:
        return None
    return project


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


def _policy_store_error_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "type": "policy_store_unavailable",
                "message": "Tenant policy enforcement is temporarily unavailable.",
            }
        },
    )


class QuotaAPI:
    """Query and manage quota enforcement state."""

    def __init__(
        self,
        quota_enforcer: QuotaEnforcer,
        policy_resolver: PolicyHierarchyResolver,
        cost_tracker: CostTracker | None = None,
    ) -> None:
        self.enforcer = quota_enforcer
        self.resolver = policy_resolver
        self.cost_tracker = cost_tracker

    async def get_project_quota(self, request: Request) -> JSONResponse:
        """GET /admin/quotas/{project_id} — current quota state for a project."""
        project_id = request.path_params["project_id"]
        environment = request.query_params.get("env")
        try:
            tenant_id = _request_tenant_id(
                request,
                request.query_params.get("tenant_id"),
            )
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        project = _authorized_project(request, project_id, tenant_id)

        try:
            policy = await self.resolver.resolve(
                project_id,
                environment,
                tenant_id=tenant_id,
                project=project,
            )
        except PolicyHierarchyStoreUnavailable:
            return _policy_store_error_response()
        # Read through to the shared counter rather than this instance's own: an
        # operator checking a budget cannot tell which task answered, and a task
        # that has not served this project since starting would report $0 for a
        # project the fleet is already blocking.
        current_spend = await self.enforcer.current_spend(
            project_id,
            tenant_id=tenant_id,
        )

        return JSONResponse(content={
            "tenant_id": tenant_id,
            "project_id": project_id,
            "environment": environment,
            "policy_limits": {
                "rate_limit_rpm": policy.rate_limit_rpm,
                "budget_limit": policy.budget_limit,
                "max_tokens_per_request": policy.max_tokens_per_request,
                "allowed_models": policy.allowed_models,
                "allowed_providers": policy.allowed_providers,
                "pii_redaction_enabled": policy.pii_redaction_enabled,
                "pii_redact_types": policy.pii_redact_types,
                # Another whitelist rebuild that dropped fields it didn't know
                # about: a project with entity detection on reported the same
                # policy as one without, so the only per-request paid feature in
                # the hierarchy was invisible here.
                "pii_reinject": policy.pii_reinject,
                "pii_ner_enabled": policy.pii_ner_enabled,
                "pii_ner_types": policy.pii_ner_types,
            },
            "usage": {
                "current_spend": round(current_spend, 4),
                "budget_remaining": round(policy.budget_limit - current_spend, 4) if policy.budget_limit is not None else None,
                "budget_utilization_pct": round((current_spend / policy.budget_limit) * 100, 1) if policy.budget_limit else None,
            },
        })

    async def reset_spend(self, request: Request) -> JSONResponse:
        """POST /admin/quotas/{project_id}/reset — reset spend counter (billing cycle)."""
        project_id = request.path_params["project_id"]
        try:
            tenant_id = _request_tenant_id(
                request,
                request.query_params.get("tenant_id"),
            )
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        old_spend = await self.enforcer.current_spend(
            project_id,
            tenant_id=tenant_id,
        )
        fleet_wide = await self.enforcer.reset_spend(
            project_id,
            tenant_id=tenant_id,
        )
        if not fleet_wide:
            # 503, not 200: the shared counter still holds the old total, so every
            # other instance keeps blocking the project. Reporting "reset" here
            # would tell an operator their unblock worked when the next request
            # will still be refused.
            return JSONResponse(
                status_code=503,
                content={
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "previous_spend": round(old_spend, 4),
                    "current_spend": round(old_spend, 4),
                    "status": "reset_failed",
                    "detail": (
                        "Local spend was cleared but the shared counter could not be "
                        "reset, so other instances still hold the old total. Retry."
                    ),
                },
            )
        if self.cost_tracker is not None:
            self.cost_tracker.clear_project_spend(
                project_id,
                tenant_id=tenant_id,
            )
        return JSONResponse(content={
            "tenant_id": tenant_id,
            "project_id": project_id,
            "previous_spend": round(old_spend, 4),
            "current_spend": 0.0,
            "status": "reset",
        })

    async def simulate_request(self, request: Request) -> JSONResponse:
        """POST /admin/quotas/simulate — test whether a request would be allowed."""
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "JSON body must be an object"},
            )
        project_id = body.get("project_id", "")
        model = body.get("model", "")
        provider = body.get("provider")
        max_tokens = body.get("max_tokens")
        estimated_cost = body.get("estimated_cost", 0.0)
        environment = body.get("environment")
        try:
            tenant_id = _request_tenant_id(
                request,
                body.get("tenant_id"),
            )
        except _TenantScopeError as exc:
            return _scope_error_response(exc)
        project = _authorized_project(request, project_id, tenant_id)

        try:
            policy = await self.resolver.resolve(
                project_id,
                environment,
                tenant_id=tenant_id,
                project=project,
            )
        except PolicyHierarchyStoreUnavailable:
            return _policy_store_error_response()
        decision = await self.enforcer.enforce_all(
            project_id=project_id,
            model=model,
            provider=provider,
            max_tokens=max_tokens,
            estimated_cost=estimated_cost,
            policy=policy,
            tenant_id=tenant_id,
            project=project,
        )

        return JSONResponse(content={
            "tenant_id": tenant_id,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "limit_type": decision.limit_type,
            "limit_value": decision.limit_value,
            "current_value": decision.current_value,
            "resolved_policy": {
                "rate_limit_rpm": policy.rate_limit_rpm,
                "budget_limit": policy.budget_limit,
                "max_tokens_per_request": policy.max_tokens_per_request,
                "allowed_models": policy.allowed_models,
                "allowed_providers": policy.allowed_providers,
            },
        })


def create_quota_routes(quota_api: QuotaAPI) -> list[Route]:
    """Create Starlette routes for quota management."""
    return [
        Route("/admin/quotas/simulate", quota_api.simulate_request, methods=["POST"]),
        Route("/admin/quotas/{project_id}", quota_api.get_project_quota, methods=["GET"]),
        Route("/admin/quotas/{project_id}/reset", quota_api.reset_spend, methods=["POST"]),
    ]
