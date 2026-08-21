"""Tenant-aware baseline authorization for AxonLLM.

This module is the mandatory RBAC floor. External policy engines may narrow an
ALLOW decision, but they must never expand a denial from this policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.gateway.models import MembershipStatus, Principal, TenantRole


class Action(Enum):
    """Stable action names shared by HTTP and AgentCore entry points."""

    MODEL_LIST = "model.list"
    INFERENCE_INVOKE = "inference.invoke"
    QUERY_SELECT = "query.select"
    QUERY_MUTATE = "query.mutate"

    TENANT_CONFIG_READ = "tenant.config.read"
    TENANT_CONFIG_WRITE = "tenant.config.write"
    MEMBERSHIP_READ = "membership.read"
    MEMBERSHIP_WRITE = "membership.write"
    API_KEY_READ = "apikey.read"
    API_KEY_MANAGE = "apikey.manage"
    POLICY_READ = "policy.read"
    POLICY_WRITE = "policy.write"
    QUOTA_READ = "quota.read"
    QUOTA_WRITE = "quota.write"
    WEBHOOK_READ = "webhook.read"
    WEBHOOK_WRITE = "webhook.write"
    USAGE_READ = "usage.read"
    USAGE_EXPORT = "usage.export"
    AUDIT_READ = "audit.read"
    AUDIT_EXPORT = "audit.export"

    PLATFORM_READ = "platform.read"
    PLATFORM_WRITE = "platform.write"
    PLATFORM_OPERATE = "platform.operate"


PLATFORM_ACTIONS = frozenset({
    Action.PLATFORM_READ,
    Action.PLATFORM_WRITE,
    Action.PLATFORM_OPERATE,
})

TENANT_MEMBER_ACTIONS = frozenset({
    Action.MODEL_LIST,
    Action.INFERENCE_INVOKE,
    Action.QUERY_SELECT,
    Action.TENANT_CONFIG_READ,
    Action.MEMBERSHIP_READ,
    Action.API_KEY_READ,
    Action.POLICY_READ,
    Action.QUOTA_READ,
    Action.WEBHOOK_READ,
    Action.USAGE_READ,
    Action.USAGE_EXPORT,
    Action.AUDIT_READ,
    Action.AUDIT_EXPORT,
})

TENANT_AUDITOR_ACTIONS = TENANT_MEMBER_ACTIONS

TENANT_ADMIN_ACTIONS = TENANT_AUDITOR_ACTIONS | frozenset({
    Action.TENANT_CONFIG_WRITE,
    Action.MEMBERSHIP_WRITE,
    Action.API_KEY_MANAGE,
    Action.POLICY_WRITE,
    Action.QUOTA_WRITE,
    Action.WEBHOOK_WRITE,
})

TENANT_WIDE_ADMIN_ACTIONS = TENANT_ADMIN_ACTIONS.difference({
    Action.MODEL_LIST,
    Action.INFERENCE_INVOKE,
    Action.QUERY_SELECT,
})

SERVICE_ACTIONS = frozenset({
    Action.MODEL_LIST,
    Action.INFERENCE_INVOKE,
    Action.QUERY_SELECT,
})

ROLE_ACTIONS: dict[TenantRole, frozenset[Action]] = {
    TenantRole.PLATFORM_ADMIN: PLATFORM_ACTIONS,
    TenantRole.TENANT_ADMIN: TENANT_ADMIN_ACTIONS,
    TenantRole.TENANT_MEMBER: TENANT_MEMBER_ACTIONS,
    TenantRole.TENANT_AUDITOR: TENANT_AUDITOR_ACTIONS,
    TenantRole.SERVICE: frozenset(),
}


@dataclass(frozen=True)
class ResourceRef:
    """A resource whose ownership is known before an operation is authorized."""

    resource_type: str
    resource_id: str
    tenant_id: str | None
    project_id: str | None = None

    def __post_init__(self) -> None:
        if not self.resource_type.strip():
            raise ValueError("resource_type must not be empty")
        if not self.resource_id.strip():
            raise ValueError("resource_id must not be empty")
        if self.tenant_id is not None and not self.tenant_id.strip():
            raise ValueError("tenant_id must be None or non-empty")
        if self.project_id is not None and not self.project_id.strip():
            raise ValueError("project_id must be None or non-empty")


@dataclass(frozen=True)
class AuthorizationDecision:
    """A structured authorization result suitable for audit logging."""

    allowed: bool
    action: str
    reason: str
    conceal_resource: bool = False
    break_glass: bool = False

    @property
    def status_code(self) -> int:
        """HTTP status that avoids exposing cross-tenant resource existence."""
        if self.allowed:
            return 200
        return 404 if self.conceal_resource else 403


class AuthorizationDenied(PermissionError):
    """Raised by ``require_authorized`` after a default-deny decision."""

    def __init__(self, decision: AuthorizationDecision):
        super().__init__(decision.reason)
        self.decision = decision


def _normalize_action(action: Action | str) -> Action | None:
    if isinstance(action, Action):
        return action
    if not isinstance(action, str):
        return None
    try:
        return Action(action)
    except ValueError:
        return None


def _service_scope_allows(principal: Principal, action: Action) -> bool:
    """Let server-held scopes narrow the non-mutating service action ceiling."""
    return action in SERVICE_ACTIONS and (
        action.value in principal.scopes
        or "*" in principal.scopes
        or f"{action.value.rsplit('.', 1)[0]}.*" in principal.scopes
    )


def authorize(
    principal: Principal,
    action: Action | str,
    resource: ResourceRef,
    *,
    break_glass_reason: str | None = None,
) -> AuthorizationDecision:
    """Evaluate the baseline tenant RBAC policy.

    Unknown actions, inactive memberships, missing tenant ownership, and absent
    project grants all deny. A platform administrator can enter a tenant only
    through an explicit break-glass decision carrying a reason.
    """
    normalized = _normalize_action(action)
    action_name = action.value if isinstance(action, Action) else str(action)
    if normalized is None:
        return AuthorizationDecision(False, action_name, "unknown_action")

    if principal.membership_status is not MembershipStatus.ACTIVE:
        return AuthorizationDecision(False, normalized.value, "membership_inactive")

    is_platform_admin = TenantRole.PLATFORM_ADMIN in principal.roles
    if normalized in PLATFORM_ACTIONS:
        if resource.tenant_id is not None:
            return AuthorizationDecision(
                False,
                normalized.value,
                "platform_resource_must_not_have_tenant",
            )
        if is_platform_admin:
            return AuthorizationDecision(True, normalized.value, "role_allowed")
        return AuthorizationDecision(False, normalized.value, "role_not_allowed")

    if resource.tenant_id is None:
        return AuthorizationDecision(
            False,
            normalized.value,
            "tenant_resource_missing_owner",
        )

    if resource.tenant_id != principal.tenant_id:
        if is_platform_admin and break_glass_reason and break_glass_reason.strip():
            if normalized is Action.QUERY_MUTATE:
                return AuthorizationDecision(
                    False,
                    normalized.value,
                    "query_mutation_not_supported",
                )
            return AuthorizationDecision(
                True,
                normalized.value,
                "break_glass_allowed",
                break_glass=True,
            )
        return AuthorizationDecision(
            False,
            normalized.value,
            "resource_not_found",
            conceal_resource=True,
        )

    if normalized is Action.QUERY_MUTATE:
        return AuthorizationDecision(
            False,
            normalized.value,
            "query_mutation_not_supported",
        )

    if (
        resource.project_id is not None
        and resource.project_id not in principal.project_ids
        and not (
            TenantRole.TENANT_ADMIN in principal.roles
            and normalized in TENANT_WIDE_ADMIN_ACTIONS
        )
    ):
        return AuthorizationDecision(
            False,
            normalized.value,
            "resource_not_found",
            conceal_resource=True,
        )

    for role in principal.roles:
        if normalized in ROLE_ACTIONS.get(role, frozenset()):
            return AuthorizationDecision(True, normalized.value, "role_allowed")

    if (
        TenantRole.SERVICE in principal.roles
        and _service_scope_allows(principal, normalized)
    ):
        return AuthorizationDecision(True, normalized.value, "scope_allowed")

    return AuthorizationDecision(False, normalized.value, "role_not_allowed")


def require_authorized(
    principal: Principal,
    action: Action | str,
    resource: ResourceRef,
    *,
    break_glass_reason: str | None = None,
) -> AuthorizationDecision:
    """Return an ALLOW decision or raise ``AuthorizationDenied``."""
    decision = authorize(
        principal,
        action,
        resource,
        break_glass_reason=break_glass_reason,
    )
    if not decision.allowed:
        raise AuthorizationDenied(decision)
    return decision
