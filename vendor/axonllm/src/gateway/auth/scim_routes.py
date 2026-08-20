"""SCIM 2.0 provisioning endpoints (RFC 7643/7644).

Exposes ``/scim/v2/Users`` and ``/scim/v2/Groups`` so an IdP can drive the
joiner/mover/leaver lifecycle. Supports the subset every major IdP (Okta, Entra
ID, OneLogin) uses to reconcile: list with ``userName eq`` filter + pagination,
GET/POST/PUT/DELETE, and PATCH (notably ``active=false`` to deprovision).

Auth: canonical deployments use ``AXON_SCIM_TENANTS``, a JSON object mapping
tenant ids to an issuer and bearer token. Legacy deployments may continue to use
one global ``AXON_SCIM_TOKEN`` while canonical identity is disabled.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.models import ScimGroup, ScimUser

if TYPE_CHECKING:
    from src.gateway.auth.scim_service import ScimStore

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


class _ScimInvalidValue(ValueError):
    pass


class _DuplicateJsonKey(ValueError):
    pass


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise _DuplicateJsonKey(key)
        document[key] = value
    return document


@dataclass(frozen=True)
class ScimCredential:
    tenant_id: str
    issuer: str
    token: str


def _scim_error(status: int, detail: str, scim_type: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return JSONResponse(body, status_code=status)


def _user_to_scim(u: ScimUser, store: ScimStore) -> dict:
    return {
        "schemas": [USER_SCHEMA],
        "id": u.id,
        "externalId": u.external_id,
        "userName": u.user_name,
        "active": u.active,
        "displayName": u.display_name,
        "emails": u.emails,
        "groups": [{"value": g} for g in store.groups_for_user(u)],
        "roles": [{"value": r} for r in store.roles_for_user(u)],
        "meta": {"resourceType": "User", "created": u.created_at.isoformat(), "lastModified": u.updated_at.isoformat()},
    }


def _group_to_scim(g: ScimGroup) -> dict:
    return {
        "schemas": [GROUP_SCHEMA],
        "id": g.id,
        "externalId": g.external_id,
        "displayName": g.display_name,
        "members": [{"value": m} for m in g.members],
        "roles": [{"value": r} for r in g.roles],
        "meta": {
            "resourceType": "Group",
            "created": g.created_at.isoformat(),
            "lastModified": g.updated_at.isoformat(),
        },
    }


def _parse_filter_username(filt: str | None) -> str | None:
    """Parse the one filter IdPs use to reconcile: ``userName eq "value"``."""
    if not filt:
        return None
    parts = filt.split(None, 2)
    if len(parts) == 3 and parts[0].lower() == "username" and parts[1].lower() == "eq":
        return parts[2].strip().strip('"')
    return None


def _active_value(value: object) -> bool:
    if not isinstance(value, bool):
        raise _ScimInvalidValue("active must be a JSON boolean")
    return value


def _resource_active(body: dict[str, Any]) -> bool:
    if "active" not in body:
        return True
    return _active_value(body["active"])


class ScimAPI:
    """SCIM 2.0 Users + Groups handlers over a ScimStore."""

    def __init__(
        self,
        store: ScimStore,
        *,
        canonical_identity_required: bool = False,
    ) -> None:
        self.store = store
        self.canonical_identity_required = canonical_identity_required

    def _configured_credentials(
        self,
    ) -> tuple[list[ScimCredential], str | None]:
        raw = os.environ.get("AXON_SCIM_TENANTS", "").strip()
        if raw:
            try:
                document = json.loads(
                    raw,
                    object_pairs_hook=_unique_json_object,
                )
            except _DuplicateJsonKey:
                return [], "AXON_SCIM_TENANTS contains duplicate object keys"
            except (TypeError, ValueError):
                return [], "AXON_SCIM_TENANTS is not valid JSON"
            if not isinstance(document, dict) or not document:
                return [], "AXON_SCIM_TENANTS must be a non-empty object"
            credentials: list[ScimCredential] = []
            seen_tenant_ids: set[str] = set()
            seen_tokens: set[str] = set()
            for tenant_id, config in document.items():
                if not isinstance(tenant_id, str) or not tenant_id.strip() or not isinstance(config, dict):
                    return [], "SCIM tenant entries must be named objects"
                normalized_tenant_id = tenant_id.strip()
                if tenant_id != normalized_tenant_id:
                    return [], "SCIM tenant identifiers must be canonical"
                if normalized_tenant_id in seen_tenant_ids:
                    return [], "SCIM tenant identifiers must be unique"
                seen_tenant_ids.add(normalized_tenant_id)
                token = config.get("token")
                issuer = config.get("issuer")
                if not isinstance(token, str) or not token.strip() or not isinstance(issuer, str) or not issuer.strip():
                    return [], ("each SCIM tenant requires non-empty token and issuer")
                if token in seen_tokens:
                    return [], "SCIM bearer tokens must be unique per tenant"
                seen_tokens.add(token)
                credentials.append(
                    ScimCredential(
                        normalized_tenant_id,
                        issuer.strip(),
                        token,
                    )
                )
            return credentials, None

        legacy_token = os.environ.get("AXON_SCIM_TOKEN", "").strip()
        if legacy_token and not self.canonical_identity_required:
            return [ScimCredential("", "", legacy_token)], None
        if legacy_token:
            return [], ("canonical identity requires tenant-bound AXON_SCIM_TENANTS credentials")
        return [], "SCIM provisioning is not enabled"

    @staticmethod
    def _presented_token(request: Request) -> str | None:
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            return None
        value = values[0]
        if not value.startswith("Bearer ") or not value[7:] or value != value.strip():
            return None
        return value[7:]

    def _guard(self, request: Request) -> JSONResponse | None:
        credentials, configuration_error = self._configured_credentials()
        if configuration_error is not None:
            return _scim_error(503, configuration_error)
        presented = self._presented_token(request)
        if presented is None:
            return _scim_error(401, "Invalid or missing SCIM bearer token")
        matches = [credential for credential in credentials if secrets.compare_digest(presented, credential.token)]
        if len(matches) != 1:
            return _scim_error(401, "Invalid or missing SCIM bearer token")
        request.state.scim_credential = matches[0]
        return None

    @staticmethod
    def _credential(request: Request) -> ScimCredential:
        return request.state.scim_credential

    async def _prepare(self, request: Request) -> JSONResponse | None:
        if (guard := self._guard(request)) is not None:
            return guard
        try:
            await self.store.ensure_tenant_current(self._credential(request).tenant_id)
        except (RuntimeError, ValueError):
            return _scim_error(
                503,
                "SCIM identity persistence is unavailable",
            )
        return None

    @staticmethod
    def _pagination(request: Request) -> tuple[int, int] | JSONResponse:
        try:
            start = int(request.query_params.get("startIndex", "1") or "1")
            count = int(request.query_params.get("count", "100") or "100")
        except ValueError:
            return _scim_error(
                400,
                "startIndex and count must be integers",
                scim_type="invalidValue",
            )
        if start < 1 or count < 0 or count > 1000:
            return _scim_error(
                400,
                "startIndex must be positive and count must be between 0 and 1000",
                scim_type="invalidValue",
            )
        return start, count

    # -- Users ---------------------------------------------------------------

    async def list_users(self, request: Request) -> JSONResponse:
        if (g := await self._prepare(request)) is not None:
            return g
        credential = self._credential(request)
        user_name = _parse_filter_username(request.query_params.get("filter"))
        pagination = self._pagination(request)
        if isinstance(pagination, JSONResponse):
            return pagination
        start, count = pagination
        page, total = self.store.list_users(
            user_name=user_name,
            start=start,
            count=count,
            tenant_id=credential.tenant_id,
        )
        return JSONResponse(
            {
                "schemas": [LIST_SCHEMA],
                "totalResults": total,
                "startIndex": start,
                "itemsPerPage": len(page),
                "Resources": [_user_to_scim(u, self.store) for u in page],
            }
        )

    async def get_user(self, request: Request) -> JSONResponse:
        if (g := await self._prepare(request)) is not None:
            return g
        credential = self._credential(request)
        u = self.store.get_user(
            request.path_params["id"],
            credential.tenant_id,
        )
        if u is None:
            return _scim_error(404, "User not found")
        return JSONResponse(_user_to_scim(u, self.store))

    async def create_user(self, request: Request) -> JSONResponse:
        if (g := await self._prepare(request)) is not None:
            return g
        from src.gateway.auth.scim_service import (
            ScimConflictError,
            ScimValidationError,
        )

        credential = self._credential(request)
        try:
            body = await request.json()
            user_name = body["userName"]
            user = ScimUser(
                id="",
                user_name=user_name,
                tenant_id=credential.tenant_id,
                issuer=credential.issuer,
                subject=body.get("externalId", ""),
                active=_resource_active(body),
                external_id=body.get("externalId"),
                display_name=body.get("displayName", ""),
                emails=body.get("emails", []),
                roles=[role.get("value") for role in body.get("roles", [])],
                groups=(
                    []
                    if self.canonical_identity_required
                    else [
                        group.get("value")
                        for group in body.get("groups", [])
                    ]
                ),
                project_id=body.get("projectId", ""),
            )
            created = await self.store.create_user(user)
        except ScimConflictError as e:
            return _scim_error(409, str(e), scim_type="uniqueness")
        except _ScimInvalidValue as exc:
            return _scim_error(400, str(exc), scim_type="invalidValue")
        except (KeyError, TypeError):
            return _scim_error(400, "userName is required", scim_type="invalidValue")
        except ScimValidationError as exc:
            return _scim_error(400, str(exc), scim_type="invalidValue")
        except RuntimeError:
            return _scim_error(503, "SCIM identity persistence is unavailable")
        return JSONResponse(_user_to_scim(created, self.store), status_code=201)

    async def replace_user(self, request: Request) -> JSONResponse:
        if (g := await self._prepare(request)) is not None:
            return g
        from src.gateway.auth.scim_service import (
            ScimConflictError,
            ScimNotFoundError,
            ScimValidationError,
        )

        credential = self._credential(request)
        try:
            body = await request.json()
            user = ScimUser(
                id="",
                user_name=body["userName"],
                tenant_id=credential.tenant_id,
                issuer=credential.issuer,
                subject=body.get("externalId", ""),
                active=_resource_active(body),
                external_id=body.get("externalId"),
                display_name=body.get("displayName", ""),
                emails=body.get("emails", []),
                roles=[role.get("value") for role in body.get("roles", [])],
                groups=(
                    []
                    if self.canonical_identity_required
                    else [
                        group.get("value")
                        for group in body.get("groups", [])
                    ]
                ),
                project_id=body.get("projectId", ""),
            )
            updated = await self.store.replace_user(
                request.path_params["id"],
                user,
                credential.tenant_id,
            )
        except ScimNotFoundError:
            return _scim_error(404, "User not found")
        except ScimConflictError as exc:
            return _scim_error(409, str(exc), scim_type="uniqueness")
        except _ScimInvalidValue as exc:
            return _scim_error(400, str(exc), scim_type="invalidValue")
        except (KeyError, TypeError, ScimValidationError) as exc:
            detail = str(exc) or "Invalid SCIM user"
            return _scim_error(400, detail, scim_type="invalidValue")
        except RuntimeError:
            return _scim_error(503, "SCIM identity persistence is unavailable")
        return JSONResponse(_user_to_scim(updated, self.store))

    async def patch_user(self, request: Request) -> JSONResponse:
        """PATCH — primarily the deprovision toggle (active=false)."""
        if (g := await self._prepare(request)) is not None:
            return g
        from src.gateway.auth.scim_service import (
            ScimConflictError,
            ScimNotFoundError,
        )

        credential = self._credential(request)
        user_id = request.path_params["id"]
        body = await request.json()
        active: bool | None = None
        for op in body.get("Operations", []):
            if op.get("op", "").lower() not in ("replace", "add"):
                continue
            value = op.get("value")
            path = (op.get("path") or "").lower()
            if path == "active":
                try:
                    active = _active_value(value)
                except _ScimInvalidValue as exc:
                    return _scim_error(400, str(exc), scim_type="invalidValue")
            elif isinstance(value, dict) and "active" in value:
                try:
                    active = _active_value(value["active"])
                except _ScimInvalidValue as exc:
                    return _scim_error(400, str(exc), scim_type="invalidValue")
        if active is None:
            return _scim_error(400, "Only the 'active' attribute is patchable", scim_type="invalidValue")
        try:
            updated = await self.store.set_user_active(
                user_id,
                active,
                credential.tenant_id,
            )
        except ScimNotFoundError:
            return _scim_error(404, "User not found")
        except ScimConflictError as exc:
            return _scim_error(409, str(exc), scim_type="uniqueness")
        except RuntimeError:
            return _scim_error(503, "SCIM identity persistence is unavailable")
        return JSONResponse(_user_to_scim(updated, self.store))

    async def delete_user(self, request: Request) -> JSONResponse | Any:
        if (g := await self._prepare(request)) is not None:
            return g
        from src.gateway.auth.scim_service import (
            ScimConflictError,
            ScimNotFoundError,
        )

        credential = self._credential(request)
        try:
            await self.store.delete_user(
                request.path_params["id"],
                credential.tenant_id,
            )
        except ScimNotFoundError:
            return _scim_error(404, "User not found")
        except ScimConflictError as exc:
            return _scim_error(409, str(exc), scim_type="uniqueness")
        except RuntimeError:
            return _scim_error(503, "SCIM identity persistence is unavailable")
        return JSONResponse(None, status_code=204)

    # -- Groups --------------------------------------------------------------

    async def list_groups(self, request: Request) -> JSONResponse:
        if (g := await self._prepare(request)) is not None:
            return g
        credential = self._credential(request)
        pagination = self._pagination(request)
        if isinstance(pagination, JSONResponse):
            return pagination
        start, count = pagination
        page, total = self.store.list_groups(
            start=start,
            count=count,
            tenant_id=credential.tenant_id,
        )
        return JSONResponse(
            {
                "schemas": [LIST_SCHEMA],
                "totalResults": total,
                "startIndex": start,
                "itemsPerPage": len(page),
                "Resources": [_group_to_scim(g) for g in page],
            }
        )

    async def get_group(self, request: Request) -> JSONResponse:
        if (g := await self._prepare(request)) is not None:
            return g
        credential = self._credential(request)
        grp = self.store.get_group(
            request.path_params["id"],
            credential.tenant_id,
        )
        if grp is None:
            return _scim_error(404, "Group not found")
        return JSONResponse(_group_to_scim(grp))

    async def create_group(self, request: Request) -> JSONResponse:
        if (g := await self._prepare(request)) is not None:
            return g
        from src.gateway.auth.scim_service import (
            ScimConflictError,
            ScimValidationError,
        )

        credential = self._credential(request)
        try:
            body = await request.json()
            group = ScimGroup(
                id="",
                display_name=body["displayName"],
                tenant_id=credential.tenant_id,
                external_id=body.get("externalId"),
                members=[member.get("value") for member in body.get("members", [])],
                roles=[role.get("value") for role in body.get("roles", [])],
            )
            created = await self.store.create_group(group)
        except ScimConflictError as exc:
            return _scim_error(409, str(exc), scim_type="uniqueness")
        except (KeyError, TypeError, ScimValidationError) as exc:
            detail = str(exc) or "displayName is required"
            return _scim_error(400, detail, scim_type="invalidValue")
        except RuntimeError:
            return _scim_error(503, "SCIM identity persistence is unavailable")
        return JSONResponse(_group_to_scim(created), status_code=201)

    async def replace_group(self, request: Request) -> JSONResponse:
        if (g := await self._prepare(request)) is not None:
            return g
        from src.gateway.auth.scim_service import (
            ScimConflictError,
            ScimNotFoundError,
            ScimValidationError,
        )

        credential = self._credential(request)
        try:
            body = await request.json()
            group = ScimGroup(
                id="",
                display_name=body["displayName"],
                tenant_id=credential.tenant_id,
                external_id=body.get("externalId"),
                members=[member.get("value") for member in body.get("members", [])],
                roles=[role.get("value") for role in body.get("roles", [])],
            )
            updated = await self.store.replace_group(
                request.path_params["id"],
                group,
                credential.tenant_id,
            )
        except ScimNotFoundError:
            return _scim_error(404, "Group not found")
        except ScimConflictError as exc:
            return _scim_error(409, str(exc), scim_type="uniqueness")
        except (KeyError, TypeError, ScimValidationError) as exc:
            detail = str(exc) or "Invalid SCIM group"
            return _scim_error(400, detail, scim_type="invalidValue")
        except RuntimeError:
            return _scim_error(503, "SCIM identity persistence is unavailable")
        return JSONResponse(_group_to_scim(updated))

    async def delete_group(self, request: Request) -> JSONResponse:
        if (g := await self._prepare(request)) is not None:
            return g
        from src.gateway.auth.scim_service import (
            ScimConflictError,
            ScimNotFoundError,
        )

        credential = self._credential(request)
        try:
            await self.store.delete_group(
                request.path_params["id"],
                credential.tenant_id,
            )
        except ScimNotFoundError:
            return _scim_error(404, "Group not found")
        except ScimConflictError as exc:
            return _scim_error(409, str(exc), scim_type="uniqueness")
        except RuntimeError:
            return _scim_error(503, "SCIM identity persistence is unavailable")
        return JSONResponse(None, status_code=204)


def create_scim_routes(api: ScimAPI) -> list[Route]:
    return [
        Route("/scim/v2/Users", api.list_users, methods=["GET"]),
        Route("/scim/v2/Users", api.create_user, methods=["POST"]),
        Route("/scim/v2/Users/{id}", api.get_user, methods=["GET"]),
        Route("/scim/v2/Users/{id}", api.replace_user, methods=["PUT"]),
        Route("/scim/v2/Users/{id}", api.patch_user, methods=["PATCH"]),
        Route("/scim/v2/Users/{id}", api.delete_user, methods=["DELETE"]),
        Route("/scim/v2/Groups", api.list_groups, methods=["GET"]),
        Route("/scim/v2/Groups", api.create_group, methods=["POST"]),
        Route("/scim/v2/Groups/{id}", api.get_group, methods=["GET"]),
        Route("/scim/v2/Groups/{id}", api.replace_group, methods=["PUT"]),
        Route("/scim/v2/Groups/{id}", api.delete_group, methods=["DELETE"]),
    ]
