"""OIDC authentication and immutable binding for gateway workloads."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.oidc import OIDCError, OIDCValidator, tenant_from_claims
from control_plane.env import configured_org_id, is_production, tenant_is_allowed
from control_plane.models.database import Gateway
from control_plane.models.scoping import get_gateway

_GATEWAY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_validator: OIDCValidator | None = None
_validator_config: tuple[str, str, str] | None = None


@dataclass(frozen=True)
class WorkloadIdentity:
    """Verified identity carried by a gateway workload token."""

    issuer: str
    subject: str
    gateway_id: str | None
    tenant_id: str
    client_id: str

    @property
    def audit_actor(self) -> str:
        gateway = self.gateway_id or self.client_id or "unbound"
        return f"gateway:{gateway}:{self.subject}"


class WorkloadOIDCUnavailableError(OIDCError):
    """Raised when the workload issuer cannot be reached or parsed."""


def workload_oidc_enabled() -> bool:
    return bool(os.environ.get("OSTIARI_WORKLOAD_OIDC_ISSUER", "").strip())


def get_workload_validator() -> OIDCValidator | None:
    """Return the dedicated workload validator, independent of user OIDC."""
    global _validator, _validator_config

    issuer = os.environ.get("OSTIARI_WORKLOAD_OIDC_ISSUER", "").strip()
    if not issuer:
        return None
    audience = os.environ.get("OSTIARI_WORKLOAD_OIDC_AUDIENCE", "").strip()
    jwks_url = os.environ.get("OSTIARI_WORKLOAD_OIDC_JWKS_URL", "").strip()
    config = (issuer, audience, jwks_url)
    if _validator is None or _validator_config != config:
        _validator = OIDCValidator(
            issuer=issuer,
            audience=audience or None,
            jwks_url=jwks_url or None,
        )
        _validator_config = config
    return _validator


def reset_workload_validator() -> None:
    global _validator, _validator_config
    _validator = None
    _validator_config = None


def identity_from_claims(claims: dict[str, Any]) -> WorkloadIdentity:
    """Normalize and validate the gateway identity claims."""
    subject = str(claims.get("sub", "")).strip()
    if not subject:
        raise OIDCError("workload token has no subject")

    claim_name = (
        os.environ.get("OSTIARI_WORKLOAD_GATEWAY_ID_CLAIM", "").strip()
        or "gateway_id"
    )
    raw_gateway_id = str(claims.get(claim_name, "")).strip()
    gateway_id = raw_gateway_id or None
    if gateway_id is not None and not _GATEWAY_ID_PATTERN.fullmatch(gateway_id):
        raise OIDCError(
            f"workload token claim '{claim_name}' is invalid"
        )

    tenant_id = tenant_from_claims(claims)
    if not tenant_is_allowed(tenant_id):
        raise OIDCError("workload tenant is not permitted by this deployment")

    issuer = str(claims.get("iss", "")).rstrip("/")
    expected_issuer = os.environ.get("OSTIARI_WORKLOAD_OIDC_ISSUER", "").rstrip("/")
    if not issuer or issuer != expected_issuer:
        raise OIDCError("workload issuer mismatch")

    client_id = str(
        claims.get("client_id") or claims.get("azp") or claims.get("client") or ""
    ).strip()
    return WorkloadIdentity(
        issuer=issuer,
        subject=subject,
        gateway_id=gateway_id,
        tenant_id=tenant_id or configured_org_id(),
        client_id=client_id,
    )


def validate_workload_token(token: str) -> WorkloadIdentity:
    validator = get_workload_validator()
    if validator is None:
        raise OIDCError("workload OIDC is not configured")
    try:
        claims = validator.validate(token)
    except OIDCError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalized at the auth boundary
        raise WorkloadOIDCUnavailableError(
            "workload identity provider is unavailable"
        ) from exc
    return identity_from_claims(claims)


def request_identity(request: Request) -> WorkloadIdentity | None:
    identity = getattr(request.state, "workload_identity", None)
    return identity if isinstance(identity, WorkloadIdentity) else None


def require_gateway_claim(
    request: Request,
    gateway_id: str,
    *,
    tenant_id: str | None = None,
) -> WorkloadIdentity | None:
    """Require the verified token to name this gateway and tenant.

    ``None`` represents the legacy development shared-key path. Production
    never reaches it because startup and middleware both require workload OIDC.
    """
    identity = request_identity(request)
    if identity is None:
        if is_production():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Gateway workload identity required",
            )
        return None
    if identity.gateway_id is not None and identity.gateway_id != gateway_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workload token is not authorized for this gateway",
        )
    if tenant_id is not None and identity.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Workload token tenant does not own this gateway",
        )
    return identity


async def bind_gateway_identity(
    db: AsyncSession,
    gateway: Gateway,
    identity: WorkloadIdentity | None,
) -> None:
    """Bind a gateway to one immutable issuer/subject pair."""
    if identity is None:
        return
    if gateway.workload_subject:
        if (
            gateway.workload_subject != identity.subject
            or gateway.workload_issuer != identity.issuer
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Gateway is bound to a different workload identity",
            )
        return
    already_bound = (
        await db.execute(
            select(Gateway).where(
                Gateway.workload_issuer == identity.issuer,
                Gateway.workload_subject == identity.subject,
                or_(
                    Gateway.org_id != gateway.org_id,
                    Gateway.id != gateway.id,
                ),
            )
        )
    ).scalar_one_or_none()
    if already_bound is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workload identity is already bound to another gateway",
        )
    gateway.workload_issuer = identity.issuer
    gateway.workload_subject = identity.subject


async def authorize_gateway(
    request: Request,
    db: AsyncSession,
    gateway_id: str,
    *,
    gateway: Gateway | None = None,
) -> Gateway:
    """Authorize the request against the persisted gateway identity binding."""
    identity = request_identity(request)
    lookup_org = identity.tenant_id if identity is not None else configured_org_id()
    gateway = gateway or await get_gateway(db, gateway_id, lookup_org)
    if gateway is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Gateway not found",
        )
    identity = require_gateway_claim(
        request,
        gateway_id,
        tenant_id=gateway.org_id,
    )
    if identity is not None and (
        gateway.workload_subject != identity.subject
        or gateway.workload_issuer != identity.issuer
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Gateway workload identity binding mismatch",
        )
    return gateway


async def authorize_reported_gateway(
    request: Request,
    db: AsyncSession,
    gateway_id: str,
) -> Gateway | None:
    """Authorize a gateway id carried in an ingest payload.

    Legacy development calls keep the historical behavior for unknown gateway
    ids. OIDC callers and every production call require a registered, bound
    gateway.
    """
    identity = require_gateway_claim(request, gateway_id)
    if identity is None and not is_production():
        return (
            await get_gateway(db, gateway_id, configured_org_id())
            if gateway_id
            else None
        )
    return await authorize_gateway(request, db, gateway_id)
