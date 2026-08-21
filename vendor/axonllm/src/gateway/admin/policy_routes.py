"""Admin API routes for hierarchical policy management."""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.auth.policy_hierarchy import PolicyHierarchyStoreUnavailable
from src.gateway.models import PolicyNode
from src.gateway.persistence import PersistenceConflictError

if TYPE_CHECKING:
    from src.gateway.auth.policy_hierarchy import PolicyHierarchyResolver


def _request_tenant_id(request: Request) -> str | None:
    context = getattr(request.state, "context", None)
    tenant_id = getattr(context, "tenant_id", None)
    return tenant_id if isinstance(tenant_id, str) and tenant_id else None


def _revision_headers(revision: int) -> dict[str, str]:
    return {
        "ETag": f'"policy-hierarchy-{revision}"',
        "X-Policy-Hierarchy-Revision": str(revision),
    }


def _store_unavailable_response() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": {
                "type": "service_unavailable",
                "code": "policy_hierarchy_store_unavailable",
                "message": "Tenant policy hierarchy is temporarily unavailable.",
            }
        },
    )


def _write_conflict_response() -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "type": "write_conflict",
                "code": "policy_hierarchy_write_conflict",
                "message": (
                    "Policy hierarchy changed concurrently; reload and retry."
                ),
            }
        },
    )


class PolicyHierarchyAPI:
    """Handles CRUD for the org > BU > project > environment policy tree."""

    def __init__(self, resolver: PolicyHierarchyResolver) -> None:
        self.resolver = resolver

    async def _nodes_for_request(
        self,
        request: Request,
    ) -> tuple[str | None, dict[str, PolicyNode], int]:
        tenant_id = _request_tenant_id(request)
        nodes, revision = await self.resolver.get_nodes(tenant_id=tenant_id)
        return tenant_id, nodes, revision

    async def list_nodes(self, request: Request) -> JSONResponse:
        """GET /admin/policies/hierarchy"""
        try:
            _, scoped_nodes, revision = await self._nodes_for_request(request)
        except PolicyHierarchyStoreUnavailable:
            return _store_unavailable_response()

        nodes = [
            {
                "node_id": n.node_id,
                "node_type": n.node_type,
                "parent_id": n.parent_id,
                "display_name": n.display_name,
                "limits": n.limits,
                "revision": revision,
            }
            for n in scoped_nodes.values()
        ]
        return JSONResponse(
            content=nodes,
            headers=_revision_headers(revision),
        )

    async def get_node(self, request: Request) -> JSONResponse:
        """GET /admin/policies/hierarchy/{node_id}"""
        node_id = request.path_params["node_id"]

        try:
            _, scoped_nodes, revision = await self._nodes_for_request(request)
        except PolicyHierarchyStoreUnavailable:
            return _store_unavailable_response()

        node = scoped_nodes.get(node_id)
        if node is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"Node '{node_id}' not found."},
            )

        children = [
            n for n in scoped_nodes.values() if n.parent_id == node_id
        ]

        return JSONResponse(
            content={
                "node_id": node.node_id,
                "node_type": node.node_type,
                "parent_id": node.parent_id,
                "display_name": node.display_name,
                "limits": node.limits,
                "revision": revision,
                "children": [
                    {"node_id": c.node_id, "node_type": c.node_type, "display_name": c.display_name}
                    for c in children
                ],
            },
            headers=_revision_headers(revision),
        )

    async def create_node(self, request: Request) -> JSONResponse:
        """POST /admin/policies/hierarchy"""
        body = await request.json()

        # Required fields, checked before construction: this handler only caught
        # ValueError from set_node, so a body missing either key raised KeyError
        # and surfaced to the caller as a 500. Every sibling admin POST returns
        # 400 for the same input.
        missing = [f for f in ("node_id", "node_type") if not body.get(f)]
        if missing:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "type": "invalid_request",
                        "message": "Field(s) required: " + ", ".join(missing),
                    }
                },
            )

        node = PolicyNode(
            node_id=body["node_id"],
            node_type=body["node_type"],
            parent_id=body.get("parent_id"),
            display_name=body.get("display_name", body["node_id"]),
            limits=body.get("limits", {}),
        )

        try:
            revision = await self.resolver.set_node(
                node,
                tenant_id=_request_tenant_id(request),
                create_only=True,
            )
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        except PersistenceConflictError:
            return _write_conflict_response()
        except PolicyHierarchyStoreUnavailable:
            return _store_unavailable_response()

        return JSONResponse(
            status_code=201,
            content={
                "node_id": node.node_id,
                "node_type": node.node_type,
                "parent_id": node.parent_id,
                "display_name": node.display_name,
                "limits": node.limits,
                "revision": revision,
            },
            headers=_revision_headers(revision),
        )

    async def update_node(self, request: Request) -> JSONResponse:
        """PUT /admin/policies/hierarchy/{node_id}"""
        node_id = request.path_params["node_id"]
        body = await request.json()

        try:
            tenant_id, scoped_nodes, _ = await self._nodes_for_request(request)
        except PolicyHierarchyStoreUnavailable:
            return _store_unavailable_response()

        existing = scoped_nodes.get(node_id)
        if existing is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"Node '{node_id}' not found."},
            )

        updated = PolicyNode(
            node_id=node_id,
            node_type=existing.node_type,
            parent_id=existing.parent_id,
            display_name=body.get("display_name", existing.display_name),
            limits=body.get("limits", existing.limits),
            created_at=existing.created_at,
        )

        try:
            revision = await self.resolver.set_node(
                updated,
                tenant_id=tenant_id,
                create_only=False,
            )
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        except PersistenceConflictError:
            return _write_conflict_response()
        except PolicyHierarchyStoreUnavailable:
            return _store_unavailable_response()

        return JSONResponse(
            content={
                "node_id": updated.node_id,
                "limits": updated.limits,
                "display_name": updated.display_name,
                "revision": revision,
            },
            headers=_revision_headers(revision),
        )

    async def resolve_effective(self, request: Request) -> JSONResponse:
        """GET /admin/policies/effective/{project_id}"""
        project_id = request.path_params["project_id"]
        environment = request.query_params.get("env")

        tenant_id = _request_tenant_id(request)
        try:
            policy = await self.resolver.resolve(
                project_id,
                environment,
                tenant_id=tenant_id,
            )
        except PolicyHierarchyStoreUnavailable:
            return _store_unavailable_response()
        revision = self.resolver.known_revision(tenant_id)

        return JSONResponse(
            content={
                "project_id": project_id,
                "environment": environment,
                "revision": revision,
                "effective_policy": {
                    "rate_limit_rpm": policy.rate_limit_rpm,
                    "budget_limit": policy.budget_limit,
                    "allowed_models": policy.allowed_models,
                    "max_tokens_per_request": policy.max_tokens_per_request,
                    "allowed_providers": policy.allowed_providers,
                },
            },
            headers=_revision_headers(revision),
        )


def create_policy_hierarchy_routes(policy_api: PolicyHierarchyAPI) -> list[Route]:
    """Create Starlette routes for policy hierarchy management."""
    return [
        Route("/admin/policies/hierarchy", policy_api.list_nodes, methods=["GET"]),
        Route("/admin/policies/hierarchy", policy_api.create_node, methods=["POST"]),
        Route("/admin/policies/hierarchy/{node_id:path}", policy_api.get_node, methods=["GET"]),
        Route("/admin/policies/hierarchy/{node_id:path}", policy_api.update_node, methods=["PUT"]),
        Route("/admin/policies/effective/{project_id}", policy_api.resolve_effective, methods=["GET"]),
    ]
