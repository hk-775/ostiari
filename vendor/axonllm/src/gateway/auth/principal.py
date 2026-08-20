"""Canonical principal resolution from authenticated credentials."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    RequestContext,
    TenantRole,
)


API_KEY_ISSUER = "urn:axonllm:api-key"


@dataclass(frozen=True)
class CredentialIdentity:
    """Verified credential identity before tenant membership resolution."""

    issuer: str
    subject: str
    auth_method: AuthMethod
    tenant_hint: str | None = None
    project_hint: str | None = None
    credential_id: str | None = None

    def __post_init__(self) -> None:
        if not self.issuer.strip():
            raise ValueError("issuer must not be empty")
        if not self.subject.strip():
            raise ValueError("subject must not be empty")
        if self.auth_method is AuthMethod.ANONYMOUS:
            raise ValueError("anonymous requests do not have a credential identity")
        if self.tenant_hint is not None and not self.tenant_hint.strip():
            raise ValueError("tenant_hint must be None or non-empty")
        if self.project_hint is not None and not self.project_hint.strip():
            raise ValueError("project_hint must be None or non-empty")

    @classmethod
    def from_request_context(cls, context: RequestContext) -> CredentialIdentity:
        """Extract identity fields while deliberately ignoring claimed authority."""
        if context.auth_method is AuthMethod.API_KEY:
            subject = context.api_key_id or context.subject
            if not subject:
                raise ValueError("API key context is missing key identity")
            return cls(
                issuer=API_KEY_ISSUER,
                subject=subject,
                auth_method=context.auth_method,
                tenant_hint=context.tenant_id,
                project_hint=context.project_id or None,
                credential_id=context.api_key_id,
            )

        issuer = context.issuer
        subject = context.subject or context.user_id
        if not issuer:
            raise ValueError("federated context is missing issuer")
        return cls(
            issuer=issuer,
            subject=subject,
            auth_method=context.auth_method,
            tenant_hint=context.tenant_id,
            project_hint=context.project_id or None,
            credential_id=context.api_key_id,
        )


class PrincipalRepository(Protocol):
    """Authoritative tenant identity store used by every ingress path."""

    async def resolve(self, identity: CredentialIdentity) -> Principal | None:
        ...


class PrincipalResolver(Protocol):
    """Middleware-facing principal resolution contract."""

    async def resolve(self, context: RequestContext) -> Principal | None:
        ...


class CanonicalPrincipalResolver:
    """Resolve claims to server-held authority and reject mismatched results."""

    def __init__(self, repository: PrincipalRepository) -> None:
        self._repository = repository

    async def resolve(self, context: RequestContext) -> Principal | None:
        try:
            identity = CredentialIdentity.from_request_context(context)
        except (TypeError, ValueError):
            return None

        principal = await self._repository.resolve(identity)
        if principal is None:
            return None
        if principal.membership_status is not MembershipStatus.ACTIVE:
            return None
        if principal.issuer != identity.issuer:
            return None
        if principal.subject != identity.subject:
            return None
        if principal.auth_method is not identity.auth_method:
            return None
        if (
            identity.tenant_hint is not None
            and principal.tenant_id != identity.tenant_hint
        ):
            return None

        unrestricted_projects = {
            TenantRole.PLATFORM_ADMIN,
        }
        if (
            identity.project_hint is not None
            and principal.roles.isdisjoint(unrestricted_projects)
            and identity.project_hint not in principal.project_ids
        ):
            return None
        return principal


class InMemoryPrincipalRepository:
    """Deterministic local repository; production must use durable storage."""

    def __init__(self, principals: list[Principal] | None = None) -> None:
        self._principals: dict[tuple[str, str, str], Principal] = {}
        for principal in principals or []:
            self.put(principal)

    def put(self, principal: Principal) -> None:
        key = (principal.issuer, principal.subject, principal.tenant_id)
        self._principals[key] = principal

    async def resolve(self, identity: CredentialIdentity) -> Principal | None:
        if identity.tenant_hint is not None:
            return self._principals.get(
                (identity.issuer, identity.subject, identity.tenant_hint)
            )

        matches = [
            principal
            for (issuer, subject, _), principal in self._principals.items()
            if issuer == identity.issuer
            and subject == identity.subject
            and principal.membership_status is MembershipStatus.ACTIVE
        ]
        return matches[0] if len(matches) == 1 else None


def canonical_request_context(
    credential_context: RequestContext,
    principal: Principal,
) -> RequestContext:
    """Replace all claim-derived authority with canonical principal values."""
    return replace(
        credential_context,
        user_id=principal.principal_id,
        roles=sorted(role.value for role in principal.roles),
        scopes=sorted(principal.scopes),
        auth_method=principal.auth_method,
        tenant_id=principal.tenant_id,
        api_key_id=principal.credential_id,
        email=principal.email,
        issuer=principal.issuer,
        subject=principal.subject,
        principal_id=principal.principal_id,
        authorization_version=principal.authorization_version,
    )
