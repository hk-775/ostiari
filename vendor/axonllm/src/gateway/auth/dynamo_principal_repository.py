"""DynamoDB-backed canonical principal repository."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from src.gateway.auth.principal import CredentialIdentity
from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    TenantRole,
)

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _dynamo_integer(value: object) -> int | None:
    """Normalize exact DynamoDB numbers while rejecting bools and fractions."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return int(value)
    return None


class PrincipalRepositoryError(RuntimeError):
    """Base error for authoritative identity-store failures."""


class PrincipalConflictError(PrincipalRepositoryError):
    """An authorization-version precondition rejected a write."""


class PrincipalStoreUnavailable(PrincipalRepositoryError):
    """Canonical identity storage is disabled or unavailable."""


def identity_partition_key(issuer: str, subject: str) -> str:
    """Return a fixed-size key without exposing identity text in DynamoDB keys."""
    digest = hashlib.sha256(f"{issuer}\0{subject}".encode()).hexdigest()
    return f"IDENTITY#{digest}"


def membership_sort_key(tenant_id: str) -> str:
    return f"TENANT#{tenant_id}"


class DynamoPrincipalRepository:
    """Resolve external identities to tenant-qualified principal records.

    Reads are strongly consistent because deprovisioning and role changes are
    authorization decisions, not eventually consistent display data.
    """

    def __init__(self, persistence: DynamoPersistence) -> None:
        self._persistence = persistence

    @property
    def enabled(self) -> bool:
        return self._persistence.enabled

    @staticmethod
    def serialize(principal: Principal) -> dict[str, Any]:
        return {
            "PK": identity_partition_key(principal.issuer, principal.subject),
            "SK": membership_sort_key(principal.tenant_id),
            "entity_type": "tenant_principal",
            "schema_version": SCHEMA_VERSION,
            "principal_id": principal.principal_id,
            "tenant_id": principal.tenant_id,
            "subject": principal.subject,
            "issuer": principal.issuer,
            "roles": sorted(role.value for role in principal.roles),
            "auth_method": principal.auth_method.value,
            "membership_status": principal.membership_status.value,
            "project_ids": sorted(principal.project_ids),
            "scopes": sorted(principal.scopes),
            "authorization_version": principal.authorization_version,
            "credential_id": principal.credential_id,
            "email": principal.email,
            # Ready for a tenant-listing GSI in the v2 identity table.
            "GSI1PK": f"TENANT#{principal.tenant_id}",
            "GSI1SK": f"PRINCIPAL#{principal.principal_id}",
        }

    @staticmethod
    def deserialize(item: dict[str, Any]) -> Principal:
        if item.get("entity_type") != "tenant_principal":
            raise ValueError("item is not a tenant principal")
        schema_version = _dynamo_integer(item.get("schema_version"))
        if schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported tenant principal schema version")

        roles_raw = item.get("roles")
        projects_raw = item.get("project_ids", [])
        scopes_raw = item.get("scopes", [])
        for name, values in (
            ("roles", roles_raw),
            ("project_ids", projects_raw),
            ("scopes", scopes_raw),
        ):
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip()
                for value in values
            ):
                raise ValueError(f"{name} must be a non-empty string list")

        required_strings: dict[str, str] = {}
        for name in (
            "PK",
            "SK",
            "principal_id",
            "tenant_id",
            "subject",
            "issuer",
            "auth_method",
            "membership_status",
        ):
            value = item.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            required_strings[name] = value

        authorization_version = _dynamo_integer(
            item.get("authorization_version")
        )
        if authorization_version is None or authorization_version < 1:
            raise ValueError("authorization_version must be a positive integer")

        optional_strings: dict[str, str | None] = {}
        for name in ("credential_id", "email"):
            value = item.get(name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{name} must be null or a non-empty string")
            optional_strings[name] = value

        return Principal(
            principal_id=required_strings["principal_id"],
            tenant_id=required_strings["tenant_id"],
            subject=required_strings["subject"],
            issuer=required_strings["issuer"],
            roles=frozenset(TenantRole(role) for role in roles_raw),
            auth_method=AuthMethod(required_strings["auth_method"]),
            membership_status=MembershipStatus(
                required_strings["membership_status"]
            ),
            project_ids=frozenset(projects_raw),
            scopes=frozenset(scopes_raw),
            authorization_version=authorization_version,
            credential_id=optional_strings["credential_id"],
            email=optional_strings["email"],
        )

    async def put(
        self,
        principal: Principal,
        *,
        expected_authorization_version: int | None = None,
    ) -> None:
        """Create or version-update a principal with an optimistic condition."""
        if not self.enabled:
            raise PrincipalStoreUnavailable(
                "canonical principal persistence is disabled"
            )
        if expected_authorization_version is None:
            if principal.authorization_version != 1:
                raise ValueError("new principals must start at authorization_version 1")
            condition = "attribute_not_exists(PK) AND attribute_not_exists(SK)"
            expression_values = None
        else:
            if expected_authorization_version < 1:
                raise ValueError("expected_authorization_version must be positive")
            if principal.authorization_version != expected_authorization_version + 1:
                raise ValueError(
                    "updated principal authorization_version must increment by one"
                )
            condition = "authorization_version = :expected_version"
            expression_values = {
                ":expected_version": expected_authorization_version,
            }

        def _put() -> None:
            kwargs: dict[str, Any] = {
                "Item": self.serialize(principal),
                "ConditionExpression": condition,
            }
            if expression_values is not None:
                kwargs["ExpressionAttributeValues"] = expression_values
            self._persistence._get_table().put_item(**kwargs)

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            if _is_conditional_failure(exc):
                raise PrincipalConflictError(
                    "principal authorization version changed"
                ) from exc
            logger.error(
                "Canonical principal write failed principal=%s tenant=%s",
                principal.principal_id,
                principal.tenant_id,
                exc_info=True,
            )
            raise PrincipalStoreUnavailable(
                "canonical principal write failed"
            ) from exc

    async def resolve(self, identity: CredentialIdentity) -> Principal | None:
        """Resolve exactly one active membership, or deny with ``None``."""
        if not self.enabled:
            raise PrincipalStoreUnavailable(
                "canonical principal persistence is disabled"
            )

        try:
            if identity.tenant_hint is not None:
                items = await self._get_exact(identity)
            else:
                items = await self._query_identity(identity)
        except Exception as exc:
            logger.error(
                "Canonical principal read failed issuer=%s",
                identity.issuer,
                exc_info=True,
            )
            raise PrincipalStoreUnavailable(
                "canonical principal read failed"
            ) from exc

        principals: list[Principal] = []
        for item in items:
            try:
                principal = self.deserialize(item)
            except (KeyError, TypeError, ValueError) as exc:
                logger.error("Malformed canonical principal row", exc_info=True)
                raise PrincipalStoreUnavailable(
                    "canonical principal row is malformed"
                ) from exc
            # Defend against malformed rows and the theoretical identity-hash
            # collision before considering any stored authority.
            if (
                principal.issuer != identity.issuer
                or principal.subject != identity.subject
                or item["PK"] != identity_partition_key(
                    principal.issuer,
                    principal.subject,
                )
                or item["SK"] != membership_sort_key(principal.tenant_id)
            ):
                raise PrincipalStoreUnavailable(
                    "canonical principal row does not match its identity key"
                )
            if principal.membership_status is MembershipStatus.ACTIVE:
                principals.append(principal)

        return principals[0] if len(principals) == 1 else None

    async def _get_exact(
        self,
        identity: CredentialIdentity,
    ) -> list[dict[str, Any]]:
        def _get() -> dict[str, Any]:
            return self._persistence._get_table().get_item(
                Key={
                    "PK": identity_partition_key(identity.issuer, identity.subject),
                    "SK": membership_sort_key(identity.tenant_hint or ""),
                },
                ConsistentRead=True,
            )

        response = await asyncio.to_thread(_get)
        item = response.get("Item")
        return [item] if isinstance(item, dict) else []

    async def _query_identity(
        self,
        identity: CredentialIdentity,
    ) -> list[dict[str, Any]]:
        def _query() -> list[dict[str, Any]]:
            table = self._persistence._get_table()
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": "PK = :identity_pk",
                "ExpressionAttributeValues": {
                    ":identity_pk": identity_partition_key(
                        identity.issuer,
                        identity.subject,
                    )
                },
                "ConsistentRead": True,
            }
            items: list[dict[str, Any]] = []
            while True:
                response = table.query(**kwargs)
                page = response.get("Items", [])
                if not isinstance(page, list):
                    raise ValueError("DynamoDB query returned malformed Items")
                items.extend(item for item in page if isinstance(item, dict))
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    return items
                kwargs["ExclusiveStartKey"] = last_key

        return await asyncio.to_thread(_query)


def _is_conditional_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    return (
        isinstance(error, dict)
        and error.get("Code") == "ConditionalCheckFailedException"
    )
