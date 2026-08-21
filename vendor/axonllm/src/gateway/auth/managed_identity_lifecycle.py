"""Managed Cognito and canonical-principal lifecycle operations."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
from typing import Any

from src.gateway.auth.dynamo_principal_repository import (
    DynamoPrincipalRepository,
    PrincipalConflictError,
)
from src.gateway.auth.principal import CredentialIdentity
from src.gateway.auth.scim_service import ScimConflictError, ScimStore
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    ScimUser,
    TenantRole,
)
from src.gateway.persistence import DynamoPersistence


COGNITO_TENANT_ATTRIBUTE = "custom:tenant_id"
COGNITO_PROJECT_ATTRIBUTE = "custom:project_id"
PLATFORM_HOME_TENANT = "platform-home"
PLATFORM_PROJECT_HINT = "platform"
MANAGED_TENANT_ROLES = frozenset(
    {
        TenantRole.TENANT_ADMIN,
        TenantRole.TENANT_MEMBER,
        TenantRole.TENANT_AUDITOR,
    }
)
_UNUSABLE_COGNITO_STATUSES = frozenset(
    {"ARCHIVED", "UNKNOWN", "RESET_REQUIRED"}
)


class ManagedIdentityError(RuntimeError):
    """Raised when a managed identity cannot be safely reconciled."""


@dataclass(frozen=True)
class CognitoIdentityResult:
    subject: str
    cognito_user_name: str
    created: bool = False
    changed: bool = False


@dataclass(frozen=True)
class ManagedIdentityResult:
    operation: str
    user_name: str
    subject: str
    tenant_id: str
    principal_id: str | None
    role: str | None
    project_ids: tuple[str, ...]
    cognito_created: bool
    cognito_changed: bool
    canonical_created: bool
    canonical_changed: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _required(value: str, name: str, *, max_length: int = 320) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(
            f"{name} must be a non-empty string without surrounding "
            "whitespace or control characters"
        )
    return value


def _email(value: str, name: str) -> str:
    value = _required(value, name)
    local, separator, domain = value.rpartition("@")
    if (
        separator != "@"
        or not local
        or not domain
        or "." not in domain
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{name} must be a valid email address")
    return value


def _tenant_role(role: TenantRole | str) -> TenantRole:
    try:
        normalized = role if isinstance(role, TenantRole) else TenantRole(role)
    except ValueError as exc:
        raise ValueError("role must be a canonical tenant role") from exc
    if normalized not in MANAGED_TENANT_ROLES:
        raise ValueError(
            "managed tenant users may only be tenant_admin, tenant_member, "
            "or tenant_auditor"
        )
    return normalized


def _projects(
    project_ids: list[str] | tuple[str, ...],
    default_project_id: str,
) -> tuple[tuple[str, ...], str]:
    default_project_id = _required(
        default_project_id,
        "default_project_id",
        max_length=128,
    )
    normalized: list[str] = []
    for value in project_ids:
        project_id = _required(value, "project_id", max_length=128)
        if project_id not in normalized:
            normalized.append(project_id)
    if not normalized:
        raise ValueError("at least one project_id is required")
    if default_project_id not in normalized:
        raise ValueError(
            "default_project_id must be included in project_ids"
        )
    return tuple(sorted(normalized)), default_project_id


def _aws_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    return code if isinstance(code, str) else None


def _attributes(raw: Any) -> dict[str, str]:
    if not isinstance(raw, list):
        raise ManagedIdentityError(
            "Cognito returned malformed user attributes"
        )
    attributes: dict[str, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ManagedIdentityError(
                "Cognito returned malformed user attributes"
            )
        name = item.get("Name")
        value = item.get("Value")
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or name in attributes
        ):
            raise ManagedIdentityError(
                "Cognito returned malformed or duplicate user attributes"
            )
        attributes[name] = value
    return attributes


def _get_cognito_user(
    client: Any,
    *,
    user_pool_id: str,
    user_name: str,
) -> dict[str, Any] | None:
    try:
        user = client.admin_get_user(
            UserPoolId=user_pool_id,
            Username=user_name,
        )
    except Exception as exc:
        if _aws_error_code(exc) == "UserNotFoundException":
            return None
        raise ManagedIdentityError(
            "could not resolve the managed Cognito user"
        ) from exc
    if not isinstance(user, dict):
        raise ManagedIdentityError("Cognito returned a malformed user")
    return user


def _verify_cognito_user(
    user: dict[str, Any],
    *,
    tenant_id: str,
    expected_attributes: dict[str, str] | None = None,
    enabled: bool = True,
) -> tuple[str, dict[str, str]]:
    if user.get("Enabled") is not enabled:
        state = "enabled" if enabled else "disabled"
        raise ManagedIdentityError(
            f"the managed Cognito user is not {state}"
        )
    if user.get("UserStatus") in _UNUSABLE_COGNITO_STATUSES:
        raise ManagedIdentityError(
            "the managed Cognito user has an unusable status"
        )
    attributes = _attributes(user.get("UserAttributes"))
    if attributes.get(COGNITO_TENANT_ATTRIBUTE) != tenant_id:
        raise ManagedIdentityError(
            "the managed Cognito user belongs to a different tenant"
        )
    if expected_attributes is not None:
        mismatches = sorted(
            name
            for name, value in expected_attributes.items()
            if attributes.get(name) != value
        )
        if mismatches:
            raise ManagedIdentityError(
                "the managed Cognito user has conflicting attributes: "
                + ", ".join(mismatches)
            )
    subject = attributes.get("sub")
    if (
        not isinstance(subject, str)
        or not subject
        or subject != subject.strip()
    ):
        raise ManagedIdentityError(
            "the managed Cognito user has no stable subject"
        )
    return subject, attributes


def _cognito_user_name(
    user: dict[str, Any],
    fallback: str,
) -> str:
    user_name = user.get("Username", fallback)
    if (
        not isinstance(user_name, str)
        or not user_name
        or user_name != user_name.strip()
    ):
        raise ManagedIdentityError(
            "the managed Cognito user has no stable username"
        )
    return user_name


def _desired_cognito_attributes(
    *,
    email: str,
    tenant_id: str,
    project_id: str,
) -> dict[str, str]:
    return {
        "email": email,
        "email_verified": "true",
        COGNITO_TENANT_ATTRIBUTE: tenant_id,
        COGNITO_PROJECT_ATTRIBUTE: project_id,
    }


def invite_cognito_identity(
    client: Any,
    *,
    user_pool_id: str,
    user_name: str,
    email: str,
    tenant_id: str,
    project_id: str,
) -> CognitoIdentityResult:
    """Invite one Cognito identity and verify exact attributes on every run."""
    user_pool_id = _required(user_pool_id, "user_pool_id")
    user_name = _email(user_name, "user_name")
    email = _email(email, "email")
    tenant_id = _required(tenant_id, "tenant_id", max_length=128)
    project_id = _required(project_id, "project_id", max_length=128)
    expected = _desired_cognito_attributes(
        email=email,
        tenant_id=tenant_id,
        project_id=project_id,
    )

    created = False
    user = _get_cognito_user(
        client,
        user_pool_id=user_pool_id,
        user_name=user_name,
    )
    if user is None:
        try:
            client.admin_create_user(
                UserPoolId=user_pool_id,
                Username=user_name,
                DesiredDeliveryMediums=["EMAIL"],
                UserAttributes=[
                    {"Name": name, "Value": value}
                    for name, value in expected.items()
                ],
            )
            created = True
        except Exception as exc:
            if _aws_error_code(exc) != "UsernameExistsException":
                raise ManagedIdentityError(
                    "could not invite the managed Cognito user"
                ) from exc
        user = _get_cognito_user(
            client,
            user_pool_id=user_pool_id,
            user_name=user_name,
        )
        if user is None:
            raise ManagedIdentityError(
                "invited Cognito user could not be resolved"
            )

    subject, _ = _verify_cognito_user(
        user,
        tenant_id=tenant_id,
        expected_attributes=expected,
    )
    return CognitoIdentityResult(
        subject=subject,
        cognito_user_name=_cognito_user_name(user, user_name),
        created=created,
    )


def read_cognito_identity(
    client: Any,
    *,
    user_pool_id: str,
    user_name: str,
    tenant_id: str,
    enabled: bool = True,
    fallback_user_name: str | None = None,
) -> CognitoIdentityResult:
    user_pool_id = _required(user_pool_id, "user_pool_id")
    user_name = _email(user_name, "user_name")
    user = _get_cognito_user(
        client,
        user_pool_id=user_pool_id,
        user_name=user_name,
    )
    if user is None and fallback_user_name is not None:
        user = _get_cognito_user(
            client,
            user_pool_id=user_pool_id,
            user_name=_required(
                fallback_user_name,
                "fallback_user_name",
            ),
        )
    if user is None:
        raise ManagedIdentityError("the managed Cognito user does not exist")
    subject, _ = _verify_cognito_user(
        user,
        tenant_id=_required(tenant_id, "tenant_id", max_length=128),
        enabled=enabled,
    )
    return CognitoIdentityResult(
        subject=subject,
        cognito_user_name=_cognito_user_name(user, user_name),
    )


def update_cognito_identity(
    client: Any,
    *,
    user_pool_id: str,
    user_name: str,
    subject: str,
    email: str,
    tenant_id: str,
    project_id: str,
) -> CognitoIdentityResult:
    """Update mutable Cognito hints without permitting tenant reassignment."""
    user_pool_id = _required(user_pool_id, "user_pool_id")
    user_name = _email(user_name, "user_name")
    subject = _required(subject, "subject")
    email = _email(email, "email")
    tenant_id = _required(tenant_id, "tenant_id", max_length=128)
    project_id = _required(project_id, "project_id", max_length=128)
    user = _get_cognito_user(
        client,
        user_pool_id=user_pool_id,
        user_name=user_name,
    )
    if user is None:
        user = _get_cognito_user(
            client,
            user_pool_id=user_pool_id,
            user_name=subject,
        )
    if user is None:
        raise ManagedIdentityError("the managed Cognito user does not exist")
    current_subject, current = _verify_cognito_user(
        user,
        tenant_id=tenant_id,
    )
    if current_subject != subject:
        raise ManagedIdentityError(
            "the managed Cognito subject changed during reconciliation"
        )
    cognito_user_name = _cognito_user_name(user, user_name)

    desired = _desired_cognito_attributes(
        email=email,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    updates = [
        {"Name": name, "Value": value}
        for name, value in desired.items()
        if current.get(name) != value
    ]
    if updates:
        try:
            client.admin_update_user_attributes(
                UserPoolId=user_pool_id,
                Username=cognito_user_name,
                UserAttributes=updates,
            )
        except Exception as exc:
            raise ManagedIdentityError(
                "could not update the managed Cognito user"
            ) from exc
        try:
            client.admin_user_global_sign_out(
                UserPoolId=user_pool_id,
                Username=cognito_user_name,
            )
        except Exception as exc:
            if _aws_error_code(exc) != "NotAuthorizedException":
                raise ManagedIdentityError(
                    "could not revoke the managed Cognito sessions"
                ) from exc

    verified = _get_cognito_user(
        client,
        user_pool_id=user_pool_id,
        user_name=cognito_user_name,
    )
    if verified is None:
        raise ManagedIdentityError(
            "updated Cognito user could not be resolved"
        )
    verified_subject, _ = _verify_cognito_user(
        verified,
        tenant_id=tenant_id,
        expected_attributes=desired,
    )
    if verified_subject != subject:
        raise ManagedIdentityError(
            "the managed Cognito subject changed during reconciliation"
        )
    return CognitoIdentityResult(
        subject=subject,
        cognito_user_name=cognito_user_name,
        changed=bool(updates),
    )


def disable_cognito_identity(
    client: Any,
    *,
    user_pool_id: str,
    user_name: str,
    tenant_id: str,
    subject_hint: str | None = None,
) -> CognitoIdentityResult:
    """Disable Cognito first so a partial deprovision fails closed."""
    user_pool_id = _required(user_pool_id, "user_pool_id")
    user_name = _email(user_name, "user_name")
    tenant_id = _required(tenant_id, "tenant_id", max_length=128)
    user = _get_cognito_user(
        client,
        user_pool_id=user_pool_id,
        user_name=user_name,
    )
    if user is None and subject_hint is not None:
        user = _get_cognito_user(
            client,
            user_pool_id=user_pool_id,
            user_name=_required(subject_hint, "subject_hint"),
        )
    if user is None:
        raise ManagedIdentityError("the managed Cognito user does not exist")
    was_enabled = user.get("Enabled") is True
    subject, _ = _verify_cognito_user(
        user,
        tenant_id=tenant_id,
        enabled=was_enabled,
    )
    if subject_hint is not None and subject != subject_hint:
        raise ManagedIdentityError(
            "the managed Cognito subject conflicts with canonical authority"
        )
    cognito_user_name = _cognito_user_name(user, user_name)
    if was_enabled:
        try:
            client.admin_disable_user(
                UserPoolId=user_pool_id,
                Username=cognito_user_name,
            )
        except Exception as exc:
            raise ManagedIdentityError(
                "could not disable the managed Cognito user"
            ) from exc
    try:
        client.admin_user_global_sign_out(
            UserPoolId=user_pool_id,
            Username=cognito_user_name,
        )
    except Exception as exc:
        if _aws_error_code(exc) != "NotAuthorizedException":
            raise ManagedIdentityError(
                "could not revoke the managed Cognito sessions"
            ) from exc

    disabled = _get_cognito_user(
        client,
        user_pool_id=user_pool_id,
        user_name=cognito_user_name,
    )
    if disabled is None:
        raise ManagedIdentityError(
            "disabled Cognito user could not be resolved"
        )
    disabled_subject, _ = _verify_cognito_user(
        disabled,
        tenant_id=tenant_id,
        enabled=False,
    )
    if disabled_subject != subject:
        raise ManagedIdentityError(
            "the managed Cognito subject changed during deprovisioning"
        )
    return CognitoIdentityResult(
        subject=subject,
        cognito_user_name=cognito_user_name,
        changed=was_enabled,
    )


def _user_projects(user: ScimUser) -> set[str]:
    projects = {
        project_id for project_id in user.project_ids if project_id.strip()
    }
    if user.project_id.strip():
        projects.add(user.project_id)
    return projects


def _validate_scim_identity(
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
        raise ManagedIdentityError(
            "the canonical user belongs to a different identity"
        )
    if user.deleted:
        raise ManagedIdentityError(
            "the canonical user has been permanently deprovisioned"
        )


def _reject_group_managed_roles(
    store: ScimStore,
    user: ScimUser,
) -> None:
    if set(store.roles_for_user(user)) != set(user.roles):
        raise ManagedIdentityError(
            "the canonical user's role is managed by a SCIM group; "
            "update that group through SCIM"
        )


def _desired_scim_user(
    existing: ScimUser,
    *,
    user_name: str,
    display_name: str,
    email: str,
    role: TenantRole,
) -> ScimUser:
    return replace(
        existing,
        user_name=user_name,
        external_id=existing.subject,
        display_name=display_name,
        emails=[{"value": email, "primary": True}],
        roles=[role.value],
    )


async def _store(
    persistence: DynamoPersistence,
    tenant_id: str,
) -> ScimStore:
    if not persistence.enabled:
        raise ManagedIdentityError(
            "managed identity lifecycle requires DynamoDB persistence"
        )
    store = ScimStore(
        persistence=persistence,
        canonical_identity_required=True,
    )
    await store.initialize()
    await store.ensure_tenant_current(tenant_id, force=True)
    return store


async def _verify_tenant_principal(
    persistence: DynamoPersistence,
    *,
    issuer: str,
    subject: str,
    tenant_id: str,
    default_project_id: str,
    role: TenantRole,
    project_ids: tuple[str, ...],
) -> Principal:
    principal = await DynamoPrincipalRepository(persistence).resolve(
        CredentialIdentity(
            issuer=issuer,
            subject=subject,
            auth_method=AuthMethod.OIDC_JWT,
            tenant_hint=tenant_id,
            project_hint=default_project_id,
        )
    )
    if (
        principal is None
        or principal.membership_status is not MembershipStatus.ACTIVE
        or principal.roles != frozenset({role})
        or principal.project_ids != frozenset(project_ids)
        or principal.tenant_id != tenant_id
        or principal.issuer != issuer
        or principal.subject != subject
    ):
        raise ManagedIdentityError(
            "canonical tenant-principal verification failed"
        )
    return principal


async def _grant_missing_projects(
    persistence: DynamoPersistence,
    *,
    tenant_id: str,
    user_id: str,
    project_ids: set[str],
) -> bool:
    changed = False
    for project_id in sorted(project_ids):
        _, membership_changed = (
            await persistence.set_tenant_project_membership(
                tenant_id,
                project_id,
                user_id,
                granted=True,
            )
        )
        changed = changed or membership_changed
    return changed


async def invite_managed_tenant_user(
    cognito_client: Any,
    persistence: DynamoPersistence,
    *,
    user_pool_id: str,
    issuer: str,
    user_name: str,
    email: str,
    display_name: str,
    tenant_id: str,
    role: TenantRole | str,
    project_ids: list[str] | tuple[str, ...],
    default_project_id: str,
) -> ManagedIdentityResult:
    """Invite or finish provisioning an exact managed tenant user."""
    issuer = _required(issuer, "issuer")
    user_name = _email(user_name, "user_name")
    email = _email(email, "email")
    display_name = (
        _required(display_name, "display_name")
        if display_name
        else ""
    )
    tenant_id = _required(tenant_id, "tenant_id", max_length=128)
    role = _tenant_role(role)
    projects, default_project_id = _projects(
        project_ids,
        default_project_id,
    )
    cognito = invite_cognito_identity(
        cognito_client,
        user_pool_id=user_pool_id,
        user_name=user_name,
        email=email,
        tenant_id=tenant_id,
        project_id=default_project_id,
    )

    store = await _store(persistence, tenant_id)
    user = store.get_user_by_username(user_name, tenant_id)
    canonical_created = False
    canonical_changed = False
    if user is None:
        try:
            user = await store.create_user(
                ScimUser(
                    id="",
                    user_name=user_name,
                    tenant_id=tenant_id,
                    issuer=issuer,
                    subject=cognito.subject,
                    external_id=cognito.subject,
                    display_name=display_name,
                    emails=[{"value": email, "primary": True}],
                    roles=[role.value],
                )
            )
        except ScimConflictError:
            await store.ensure_tenant_current(tenant_id, force=True)
            user = store.get_user_by_username(user_name, tenant_id)
            if user is None:
                raise
        else:
            canonical_created = True

    if canonical_created:
        current_projects: set[str] = set()
    else:
        assert user is not None
        _validate_scim_identity(
            user,
            tenant_id=tenant_id,
            issuer=issuer,
            subject=cognito.subject,
        )
        _reject_group_managed_roles(store, user)
        if (
            not user.active
            or user.user_name != user_name
            or user.display_name != display_name
            or user.primary_email != email
            or user.roles != [role.value]
        ):
            raise ManagedIdentityError(
                "the existing canonical user conflicts with the invitation"
            )
        current_projects = _user_projects(user)
        extra_projects = current_projects.difference(projects)
        if extra_projects:
            raise ManagedIdentityError(
                "the existing canonical user has conflicting project grants: "
                + ", ".join(sorted(extra_projects))
            )

    canonical_changed = await _grant_missing_projects(
        persistence,
        tenant_id=tenant_id,
        user_id=user.id,
        project_ids=set(projects).difference(current_projects),
    )
    principal = await _verify_tenant_principal(
        persistence,
        issuer=issuer,
        subject=cognito.subject,
        tenant_id=tenant_id,
        default_project_id=default_project_id,
        role=role,
        project_ids=projects,
    )
    return ManagedIdentityResult(
        operation="invite-user",
        user_name=user_name,
        subject=cognito.subject,
        tenant_id=tenant_id,
        principal_id=principal.principal_id,
        role=role.value,
        project_ids=projects,
        cognito_created=cognito.created,
        cognito_changed=False,
        canonical_created=canonical_created,
        canonical_changed=canonical_changed,
    )


async def update_managed_tenant_user(
    cognito_client: Any,
    persistence: DynamoPersistence,
    *,
    user_pool_id: str,
    issuer: str,
    user_name: str,
    email: str,
    display_name: str,
    tenant_id: str,
    role: TenantRole | str,
    project_ids: list[str] | tuple[str, ...],
    default_project_id: str,
) -> ManagedIdentityResult:
    """Reconcile one active user without allowing identity or tenant moves."""
    issuer = _required(issuer, "issuer")
    user_name = _email(user_name, "user_name")
    email = _email(email, "email")
    display_name = (
        _required(display_name, "display_name")
        if display_name
        else ""
    )
    tenant_id = _required(tenant_id, "tenant_id", max_length=128)
    role = _tenant_role(role)
    projects, default_project_id = _projects(
        project_ids,
        default_project_id,
    )
    store = await _store(persistence, tenant_id)
    user = store.get_user_by_username(user_name, tenant_id)
    if user is None:
        raise ManagedIdentityError("the canonical user does not exist")
    cognito = read_cognito_identity(
        cognito_client,
        user_pool_id=user_pool_id,
        user_name=user_name,
        tenant_id=tenant_id,
        fallback_user_name=user.subject,
    )
    _validate_scim_identity(
        user,
        tenant_id=tenant_id,
        issuer=issuer,
        subject=cognito.subject,
    )
    if not user.active:
        raise ManagedIdentityError(
            "disabled users cannot be updated; invite a replacement identity"
        )
    _reject_group_managed_roles(store, user)

    canonical_changed = False
    desired_user = _desired_scim_user(
        user,
        user_name=user_name,
        display_name=display_name,
        email=email,
        role=role,
    )
    if (
        user.display_name != desired_user.display_name
        or user.primary_email != desired_user.primary_email
        or user.roles != desired_user.roles
        or user.external_id != desired_user.external_id
    ):
        user = await store.replace_user(
            user.id,
            desired_user,
            tenant_id,
        )
        canonical_changed = True

    current_projects = _user_projects(user)
    for project_id in sorted(current_projects.difference(projects)):
        _, membership_changed = (
            await persistence.set_tenant_project_membership(
                tenant_id,
                project_id,
                user.id,
                granted=False,
            )
        )
        canonical_changed = canonical_changed or membership_changed
    grants_changed = await _grant_missing_projects(
        persistence,
        tenant_id=tenant_id,
        user_id=user.id,
        project_ids=set(projects).difference(current_projects),
    )
    canonical_changed = canonical_changed or grants_changed

    principal = await _verify_tenant_principal(
        persistence,
        issuer=issuer,
        subject=cognito.subject,
        tenant_id=tenant_id,
        default_project_id=default_project_id,
        role=role,
        project_ids=projects,
    )
    updated_cognito = update_cognito_identity(
        cognito_client,
        user_pool_id=user_pool_id,
        user_name=user_name,
        subject=cognito.subject,
        email=email,
        tenant_id=tenant_id,
        project_id=default_project_id,
    )
    return ManagedIdentityResult(
        operation="update-user",
        user_name=user_name,
        subject=cognito.subject,
        tenant_id=tenant_id,
        principal_id=principal.principal_id,
        role=role.value,
        project_ids=projects,
        cognito_created=False,
        cognito_changed=updated_cognito.changed,
        canonical_created=False,
        canonical_changed=canonical_changed,
    )


async def disable_managed_tenant_user(
    cognito_client: Any,
    persistence: DynamoPersistence,
    *,
    user_pool_id: str,
    issuer: str,
    user_name: str,
    tenant_id: str,
) -> ManagedIdentityResult:
    """Disable authentication, deprovision authority, and remove all grants."""
    issuer = _required(issuer, "issuer")
    user_name = _email(user_name, "user_name")
    tenant_id = _required(tenant_id, "tenant_id", max_length=128)
    store = await _store(persistence, tenant_id)
    user = store.get_user_by_username(user_name, tenant_id)
    if user is not None and (
        user.tenant_id != tenant_id or user.issuer != issuer
    ):
        raise ManagedIdentityError(
            "the canonical user belongs to a different identity"
        )
    cognito = disable_cognito_identity(
        cognito_client,
        user_pool_id=user_pool_id,
        user_name=user_name,
        tenant_id=tenant_id,
        subject_hint=user.subject if user is not None else None,
    )

    if user is None:
        return ManagedIdentityResult(
            operation="disable-user",
            user_name=user_name,
            subject=cognito.subject,
            tenant_id=tenant_id,
            principal_id=None,
            role=None,
            project_ids=(),
            cognito_created=False,
            cognito_changed=cognito.changed,
            canonical_created=False,
            canonical_changed=False,
        )
    _validate_scim_identity(
        user,
        tenant_id=tenant_id,
        issuer=issuer,
        subject=cognito.subject,
    )

    role_names = store.roles_for_user(user)
    project_ids = _user_projects(user)
    canonical_changed = user.active
    user = await store.set_user_active(user.id, False, tenant_id)
    for project_id in sorted(project_ids):
        _, membership_changed = (
            await persistence.set_tenant_project_membership(
                tenant_id,
                project_id,
                user.id,
                granted=False,
            )
        )
        canonical_changed = canonical_changed or membership_changed

    await store.ensure_tenant_current(tenant_id, force=True)
    disabled_user = store.get_user_by_username(user_name, tenant_id)
    if (
        disabled_user is None
        or disabled_user.active
        or _user_projects(disabled_user)
    ):
        raise ManagedIdentityError(
            "canonical user deprovisioning verification failed"
        )
    resolved = await DynamoPrincipalRepository(persistence).resolve(
        CredentialIdentity(
            issuer=issuer,
            subject=cognito.subject,
            auth_method=AuthMethod.OIDC_JWT,
            tenant_hint=tenant_id,
        )
    )
    if resolved is not None:
        raise ManagedIdentityError(
            "disabled canonical principal still resolves"
        )
    return ManagedIdentityResult(
        operation="disable-user",
        user_name=user_name,
        subject=cognito.subject,
        tenant_id=tenant_id,
        principal_id=f"scim:{user.id}",
        role=role_names[0] if len(role_names) == 1 else None,
        project_ids=(),
        cognito_created=False,
        cognito_changed=cognito.changed,
        canonical_created=False,
        canonical_changed=canonical_changed,
    )


def _platform_principal_id(issuer: str, subject: str) -> str:
    digest = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
    return f"platform:{digest[:32]}"


def _validate_platform_principal(
    principal: Principal,
    candidate: Principal,
) -> None:
    if principal != candidate:
        raise ManagedIdentityError(
            "the existing platform principal conflicts with the requested "
            "operator"
        )


async def bootstrap_platform_operator(
    cognito_client: Any,
    persistence: DynamoPersistence,
    *,
    user_pool_id: str,
    issuer: str,
    user_name: str,
    email: str,
    platform_tenant_id: str = PLATFORM_HOME_TENANT,
    project_hint: str = PLATFORM_PROJECT_HINT,
) -> ManagedIdentityResult:
    """Create a dedicated non-SCIM platform operator and verify its authority."""
    if not persistence.enabled:
        raise ManagedIdentityError(
            "platform-operator bootstrap requires DynamoDB persistence"
        )
    issuer = _required(issuer, "issuer")
    user_name = _email(user_name, "user_name")
    email = _email(email, "email")
    platform_tenant_id = _required(
        platform_tenant_id,
        "platform_tenant_id",
        max_length=128,
    )
    project_hint = _required(
        project_hint,
        "project_hint",
        max_length=128,
    )
    cognito = invite_cognito_identity(
        cognito_client,
        user_pool_id=user_pool_id,
        user_name=user_name,
        email=email,
        tenant_id=platform_tenant_id,
        project_id=project_hint,
    )
    candidate = Principal(
        principal_id=_platform_principal_id(
            issuer,
            cognito.subject,
        ),
        tenant_id=platform_tenant_id,
        subject=cognito.subject,
        issuer=issuer,
        roles=frozenset({TenantRole.PLATFORM_ADMIN}),
        auth_method=AuthMethod.OIDC_JWT,
        membership_status=MembershipStatus.ACTIVE,
        project_ids=frozenset(),
        scopes=frozenset(),
        authorization_version=1,
        email=email,
    )
    repository = DynamoPrincipalRepository(persistence)
    identity = CredentialIdentity(
        issuer=issuer,
        subject=cognito.subject,
        auth_method=AuthMethod.OIDC_JWT,
        tenant_hint=platform_tenant_id,
        project_hint=project_hint,
    )
    principal = await repository.resolve(identity)
    canonical_created = False
    if principal is None:
        try:
            await repository.put(candidate)
            canonical_created = True
        except PrincipalConflictError:
            principal = await repository.resolve(identity)
            if principal is None:
                raise ManagedIdentityError(
                    "an inactive or conflicting platform principal already "
                    "exists"
                ) from None
        else:
            principal = await repository.resolve(identity)
    if principal is None:
        raise ManagedIdentityError(
            "platform-principal verification failed"
        )
    _validate_platform_principal(principal, candidate)
    return ManagedIdentityResult(
        operation="bootstrap-operator",
        user_name=user_name,
        subject=cognito.subject,
        tenant_id=platform_tenant_id,
        principal_id=principal.principal_id,
        role=TenantRole.PLATFORM_ADMIN.value,
        project_ids=(),
        cognito_created=cognito.created,
        cognito_changed=False,
        canonical_created=canonical_created,
        canonical_changed=False,
    )
