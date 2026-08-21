"""Canonical tenant and first-administrator provisioning."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from src.gateway.auth.dynamo_principal_repository import (
    DynamoPrincipalRepository,
)
from src.gateway.auth.principal import CredentialIdentity
from src.gateway.auth.scim_service import (
    ScimConflictError,
    ScimStore,
)
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Project,
    ScimUser,
    TenantRole,
)
from src.gateway.persistence import DynamoPersistence


class TenantBootstrapError(RuntimeError):
    """Raised when bootstrap cannot prove the resulting canonical authority."""


@dataclass(frozen=True)
class TenantBootstrapResult:
    tenant_id: str
    project_id: str
    project_created: bool
    project_revision: int
    scim_user_id: str
    scim_user_created: bool
    principal_id: str
    membership_changed: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty string without surrounding whitespace")
    return value


async def _project(
    persistence: DynamoPersistence,
    *,
    tenant_id: str,
    project_id: str,
    project_name: str,
    budget_limit: float | None,
) -> tuple[Project, bool]:
    existing = await persistence.get_project(project_id, tenant_id)
    if existing is not None:
        if existing.tenant_id != tenant_id:
            raise TenantBootstrapError("project lookup returned a foreign tenant")
        return existing, False

    candidate = Project(
        project_id=project_id,
        name=project_name,
        tenant_id=tenant_id,
        budget_limit=budget_limit,
    )
    try:
        await persistence.create_project(candidate)
    except ValueError:
        # A concurrent bootstrap may have won the conditional create.
        existing = await persistence.get_project(project_id, tenant_id)
        if existing is None or existing.tenant_id != tenant_id:
            raise
        return existing, False

    created = await persistence.get_project(project_id, tenant_id)
    if created is None or created.tenant_id != tenant_id:
        raise TenantBootstrapError("created project could not be strongly resolved")
    return created, True


def _validate_existing_admin(
    user: ScimUser,
    *,
    tenant_id: str,
    issuer: str,
    subject: str,
) -> None:
    if (
        user.tenant_id != tenant_id
        or user.issuer != issuer
        or user.subject != subject
    ):
        raise TenantBootstrapError(
            "SCIM userName already belongs to a different canonical identity"
        )
    if not user.active or user.deleted:
        raise TenantBootstrapError("existing bootstrap administrator is inactive")
    if TenantRole.TENANT_ADMIN.value not in user.roles:
        raise TenantBootstrapError(
            "existing canonical identity is not a tenant administrator"
        )


async def _admin_user(
    store: ScimStore,
    *,
    tenant_id: str,
    issuer: str,
    subject: str,
    user_name: str,
    display_name: str,
    email: str | None,
) -> tuple[ScimUser, bool]:
    existing = store.get_user_by_username(user_name, tenant_id)
    if existing is not None:
        _validate_existing_admin(
            existing,
            tenant_id=tenant_id,
            issuer=issuer,
            subject=subject,
        )
        return existing, False

    candidate = ScimUser(
        id="",
        user_name=user_name,
        tenant_id=tenant_id,
        issuer=issuer,
        subject=subject,
        external_id=subject,
        display_name=display_name,
        emails=(
            [{"value": email, "primary": True}]
            if email is not None
            else []
        ),
        roles=[TenantRole.TENANT_ADMIN.value],
        project_id="",
        project_ids=[],
    )
    try:
        return await store.create_user(candidate), True
    except ScimConflictError:
        # Resolve a concurrent create and accept it only if every identity and
        # authority field matches the requested bootstrap administrator.
        await store.ensure_tenant_current(tenant_id, force=True)
        existing = store.get_user_by_username(user_name, tenant_id)
        if existing is None:
            raise
        _validate_existing_admin(
            existing,
            tenant_id=tenant_id,
            issuer=issuer,
            subject=subject,
        )
        return existing, False


async def bootstrap_tenant(
    persistence: DynamoPersistence,
    *,
    tenant_id: str,
    project_id: str,
    project_name: str,
    issuer: str,
    subject: str,
    user_name: str,
    display_name: str = "",
    email: str | None = None,
    budget_limit: float | None = None,
) -> TenantBootstrapResult:
    """Create or verify a tenant project and its first canonical administrator.

    The operation is restartable. Conditional writes prevent replacement of
    existing authority, and a successful return means the final principal was
    strongly resolved from DynamoDB with the requested role and project grant.
    """
    if not persistence.enabled:
        raise TenantBootstrapError("canonical tenant bootstrap requires DynamoDB persistence")

    tenant_id = _required(tenant_id, "tenant_id")
    project_id = _required(project_id, "project_id")
    project_name = _required(project_name, "project_name")
    issuer = _required(issuer, "issuer")
    subject = _required(subject, "subject")
    user_name = _required(user_name, "user_name")
    if display_name:
        display_name = _required(display_name, "display_name")
    if email is not None:
        email = _required(email, "email")
    if budget_limit is not None and budget_limit < 0:
        raise ValueError("budget_limit must not be negative")

    project, project_created = await _project(
        persistence,
        tenant_id=tenant_id,
        project_id=project_id,
        project_name=project_name,
        budget_limit=budget_limit,
    )

    store = ScimStore(
        persistence=persistence,
        canonical_identity_required=True,
    )
    await store.initialize()
    await store.ensure_tenant_current(tenant_id, force=True)
    user, user_created = await _admin_user(
        store,
        tenant_id=tenant_id,
        issuer=issuer,
        subject=subject,
        user_name=user_name,
        display_name=display_name,
        email=email,
    )

    granted_project, membership_changed = (
        await persistence.set_tenant_project_membership(
            tenant_id,
            project_id,
            user.id,
            granted=True,
        )
    )
    principal = await DynamoPrincipalRepository(persistence).resolve(
        CredentialIdentity(
            issuer=issuer,
            subject=subject,
            auth_method=AuthMethod.OIDC_JWT,
            tenant_hint=tenant_id,
            project_hint=project_id,
        )
    )
    if (
        principal is None
        or principal.membership_status is not MembershipStatus.ACTIVE
        or principal.roles != frozenset({TenantRole.TENANT_ADMIN})
        or project_id not in principal.project_ids
        or principal.tenant_id != tenant_id
        or principal.issuer != issuer
        or principal.subject != subject
    ):
        raise TenantBootstrapError(
            "bootstrap completed writes but canonical principal verification failed"
        )
    if (
        granted_project.tenant_id != tenant_id
        or granted_project.project_id != project_id
        or principal.principal_id not in granted_project.members
    ):
        raise TenantBootstrapError("bootstrap project membership verification failed")

    return TenantBootstrapResult(
        tenant_id=tenant_id,
        project_id=project_id,
        project_created=project_created,
        project_revision=granted_project.revision,
        scim_user_id=user.id,
        scim_user_created=user_created,
        principal_id=principal.principal_id,
        membership_changed=membership_changed,
    )
