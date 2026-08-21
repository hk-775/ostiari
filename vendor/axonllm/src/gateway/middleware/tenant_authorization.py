"""Data-plane authorization for canonical tenant principals."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from src.gateway.auth.authorization import Action, ResourceRef, authorize
from src.gateway.auth.project_repository import ProjectStoreUnavailable

if TYPE_CHECKING:
    from src.gateway.auth.project_repository import ProjectResolver


DATA_PLANE_ACTIONS: dict[tuple[str, str], Action] = {
    ("GET", "/api/models"): Action.MODEL_LIST,
    ("GET", "/v1/models"): Action.MODEL_LIST,
    ("POST", "/api/chat"): Action.INFERENCE_INVOKE,
    ("POST", "/api/chat/stream"): Action.INFERENCE_INVOKE,
    ("POST", "/v1/chat/completions"): Action.INFERENCE_INVOKE,
    ("POST", "/v1/responses"): Action.INFERENCE_INVOKE,
    ("POST", "/v1/embeddings"): Action.INFERENCE_INVOKE,
    ("POST", "/v1/query"): Action.QUERY_SELECT,
}

_CANONICAL_API_PREFIXES = ("/api/", "/v1/")


class TenantAuthorizationMiddleware(BaseHTTPMiddleware):
    """Enforce the baseline RBAC floor after canonical authentication."""

    def __init__(
        self,
        app,
        *,
        project_resolver: ProjectResolver | None = None,
        require_tenant_project: bool = False,
    ) -> None:
        super().__init__(app)
        self.project_resolver = project_resolver
        self.require_tenant_project = require_tenant_project

    async def dispatch(self, request: Request, call_next) -> Response:
        # Legacy migration mode has no Principal. AuthMiddleware is responsible
        # for requiring one once AXON_REQUIRE_CANONICAL_IDENTITY is enabled.
        principal = getattr(request.state, "principal", None)
        if principal is None:
            return await call_next(request)

        method = request.method.upper()
        if method == "HEAD":
            method = "GET"
        action = DATA_PLANE_ACTIONS.get((method, request.url.path))
        if action is None:
            if request.url.path.startswith(_CANONICAL_API_PREFIXES):
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "type": "authorization_error",
                            "message": (
                                "No canonical authorization action is mapped "
                                "for this endpoint."
                            ),
                            "code": "canonical_action_required",
                        }
                    },
                )
            return await call_next(request)

        context = request.state.context
        project_id = context.project_id or None
        if project_id is None:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "type": "invalid_request",
                        "message": (
                            "An explicit project context is required for "
                            "canonical data-plane requests."
                        ),
                        "code": "project_context_required",
                    }
                },
            )
        project = None
        if self.require_tenant_project:
            if self.project_resolver is None:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "type": "authorization_error",
                            "message": (
                                "Tenant project ownership is temporarily "
                                "unavailable."
                            ),
                            "code": "project_resolver_unavailable",
                        }
                    },
                )
            try:
                project = await self.project_resolver.resolve(
                    principal.tenant_id,
                    project_id,
                )
            except ProjectStoreUnavailable:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": {
                            "type": "authorization_error",
                            "message": (
                                "Tenant project ownership is temporarily "
                                "unavailable."
                            ),
                            "code": "project_resolver_unavailable",
                        }
                    },
                )
            if project is None:
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": {
                            "type": "authorization_error",
                            "message": "The requested resource was not found.",
                            "code": "resource_not_found",
                        }
                    },
                )

        resource = ResourceRef(
            resource_type="project",
            resource_id=project_id,
            tenant_id=(
                project.tenant_id
                if project is not None
                else principal.tenant_id
            ),
            project_id=project_id,
        )
        decision = authorize(principal, action, resource)
        if decision.allowed:
            if project is not None:
                request.state.context.authorized_project = project
            request.state.authorization_decision = decision
            return await call_next(request)

        return JSONResponse(
            status_code=decision.status_code,
            content={
                "error": {
                    "type": "authorization_error",
                    "message": "The principal is not authorized for this action.",
                    "code": decision.reason,
                }
            },
        )
