"""SCIM 2.0 provisioning store — IdP-driven user/group lifecycle.

Backs the ``/scim/v2`` endpoints. An identity provider (Okta, Entra ID, etc.)
creates/updates/deactivates users and groups here; the gateway then resolves a
provisioned user's groups → roles when authenticating that user.

In canonical-identity mode every object is tenant-qualified and every user
mutation updates the authoritative Principal in the same DynamoDB transaction.
The in-memory maps are changed only after durable persistence succeeds.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.gateway.models import (
    AuthMethod,
    MembershipStatus,
    Principal,
    ScimGroup,
    ScimUser,
    TenantRole,
)

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)


class ScimConflictError(Exception):
    """Raised when creating a user whose userName already exists (SCIM 409)."""


class ScimNotFoundError(Exception):
    """Raised when a referenced resource id doesn't exist (SCIM 404)."""


class ScimValidationError(ValueError):
    """Raised when a SCIM resource cannot produce canonical authority."""


class ScimStore:
    """CRUD + lookup for SCIM Users and Groups."""

    SCIM_SYNC_TTL_SECONDS = 5.0

    _SCIM_ROLES = frozenset(
        {
            TenantRole.TENANT_ADMIN,
            TenantRole.TENANT_MEMBER,
            TenantRole.TENANT_AUDITOR,
        }
    )

    def __init__(
        self,
        persistence: DynamoPersistence | None = None,
        *,
        canonical_identity_required: bool = False,
    ) -> None:
        self._persistence = persistence
        self._canonical_identity_required = canonical_identity_required
        self._users: dict[tuple[str, str], ScimUser] = {}
        self._groups: dict[tuple[str, str], ScimGroup] = {}
        self._username_index: dict[tuple[str, str], str] = {}
        self._tenant_hydrated: set[str] = set()
        self._tenant_known_versions: dict[str, int] = {}
        self._tenant_last_version_checks: dict[str, float] = {}
        self._tenant_refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._tenant_mutation_generations: dict[str, int] = {}
        self._legacy_hydrated = False
        self._legacy_refresh_task: asyncio.Task[None] | None = None
        self._legacy_mutation_generation = 0

    @staticmethod
    def _resource_key(tenant_id: str, resource_id: str) -> tuple[str, str]:
        return tenant_id, resource_id

    @staticmethod
    def _username_key(tenant_id: str, user_name: str) -> tuple[str, str]:
        return tenant_id, user_name.casefold()

    def _validate_tenant_resource(self, tenant_id: str) -> None:
        if self._canonical_identity_required and (not tenant_id.strip() or tenant_id != tenant_id.strip()):
            raise ScimValidationError("tenant-qualified SCIM identity is required")

    # -- lifecycle -----------------------------------------------------------

    async def initialize(self) -> None:
        """Validate canonical convergence contracts without scanning identity."""
        if not self._canonical_identity_required:
            return
        self._tenant_convergence_contract()

    async def ensure_tenant_current(
        self,
        tenant_id: str,
        *,
        force: bool = False,
    ) -> None:
        """Lazily hydrate one tenant and poll its durable change version."""
        self._validate_tenant_resource(tenant_id)
        if not (self._persistence and self._persistence.enabled):
            if self._canonical_identity_required:
                raise RuntimeError("canonical SCIM persistence is disabled")
            return

        if not tenant_id:
            await self._ensure_legacy_hydrated(force=force)
            return

        now = time.monotonic()
        last_check = self._tenant_last_version_checks.get(
            tenant_id,
            float("-inf"),
        )
        if not force and tenant_id in self._tenant_hydrated and now - last_check < self.SCIM_SYNC_TTL_SECONDS:
            return

        task = self._tenant_refresh_tasks.get(tenant_id)
        if task is None or task.done():
            task = asyncio.create_task(self._refresh_tenant(tenant_id, checked_at=now))
            self._tenant_refresh_tasks[tenant_id] = task
        await asyncio.shield(task)

    def _tenant_convergence_contract(self):
        persistence = self._persistence
        if persistence is None or not persistence.enabled:
            raise RuntimeError("canonical SCIM persistence is disabled")
        version_reader = getattr(
            persistence,
            "get_tenant_scim_version",
            None,
        )
        snapshot_loader = getattr(
            persistence,
            "load_tenant_scim_snapshot_or_none",
            None,
        )
        if not callable(version_reader) or not callable(snapshot_loader):
            raise RuntimeError("tenant SCIM convergence persistence is unavailable")
        return version_reader, snapshot_loader

    async def _refresh_tenant(
        self,
        tenant_id: str,
        *,
        checked_at: float,
    ) -> None:
        version_reader, snapshot_loader = self._tenant_convergence_contract()
        known_version = self._tenant_known_versions.get(tenant_id)
        mutation_generation = self._tenant_mutation_generations.get(
            tenant_id,
            0,
        )

        try:
            version = await version_reader(tenant_id)
        except Exception as exc:
            raise RuntimeError("SCIM tenant version read failed") from exc
        self._validate_durable_version(version)

        if tenant_id in self._tenant_hydrated and version == known_version:
            self._tenant_last_version_checks[tenant_id] = checked_at
            return

        try:
            snapshot = await snapshot_loader(tenant_id)
        except Exception as exc:
            raise RuntimeError("SCIM tenant snapshot read failed") from exc
        if snapshot is None:
            raise RuntimeError("SCIM tenant snapshot read failed")

        try:
            confirmed_version = await version_reader(tenant_id)
        except Exception as exc:
            raise RuntimeError("SCIM tenant version confirmation failed") from exc
        self._validate_durable_version(confirmed_version)
        if confirmed_version != version:
            raise RuntimeError("SCIM tenant changed while its snapshot was loading")

        users, groups, usernames = self._validated_snapshot(
            tenant_id,
            snapshot,
        )
        if (
            self._tenant_known_versions.get(tenant_id) != known_version
            or self._tenant_mutation_generations.get(tenant_id, 0) != mutation_generation
        ):
            logger.info(
                "Discarding stale SCIM refresh for tenant=%r",
                tenant_id,
            )
            return

        self._replace_tenant_state(
            tenant_id,
            users=users,
            groups=groups,
            usernames=usernames,
        )
        self._tenant_known_versions[tenant_id] = version
        self._tenant_last_version_checks[tenant_id] = checked_at
        self._tenant_hydrated.add(tenant_id)

    @staticmethod
    def _validate_durable_version(version: object) -> None:
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise RuntimeError("SCIM tenant version read failed")

    def _validated_snapshot(
        self,
        tenant_id: str,
        snapshot: object,
    ) -> tuple[
        dict[tuple[str, str], ScimUser],
        dict[tuple[str, str], ScimGroup],
        dict[tuple[str, str], str],
    ]:
        if (
            not isinstance(snapshot, tuple)
            or len(snapshot) != 2
            or not isinstance(snapshot[0], list)
            or not isinstance(snapshot[1], list)
        ):
            raise RuntimeError("SCIM tenant snapshot is malformed")

        users: dict[tuple[str, str], ScimUser] = {}
        groups: dict[tuple[str, str], ScimGroup] = {}
        usernames: dict[tuple[str, str], str] = {}
        for user in snapshot[0]:
            if not isinstance(user, ScimUser) or user.tenant_id != tenant_id:
                raise RuntimeError("SCIM tenant snapshot contains a foreign user")
            if user.deleted:
                continue
            if (
                not user.id
                or not user.user_name
                or (self._canonical_identity_required and (not user.issuer or not user.subject))
            ):
                raise RuntimeError("SCIM tenant snapshot contains an invalid user")
            if self._canonical_identity_required:
                self._canonical_roles(user.roles)
                # RFC 7643 defines User.groups as readOnly. Canonical group
                # membership is represented only by Group.members.
                user = replace(user, groups=[])
            resource_key = self._resource_key(tenant_id, user.id)
            username_key = self._username_key(
                tenant_id,
                user.user_name,
            )
            if resource_key in users or username_key in usernames:
                raise RuntimeError("SCIM tenant snapshot contains duplicate users")
            users[resource_key] = user
            usernames[username_key] = user.id

        for group in snapshot[1]:
            if not isinstance(group, ScimGroup) or group.tenant_id != tenant_id:
                raise RuntimeError("SCIM tenant snapshot contains a foreign group")
            if group.deleted:
                continue
            if not group.id or not group.display_name:
                raise RuntimeError("SCIM tenant snapshot contains an invalid group")
            if self._canonical_identity_required:
                self._canonical_roles(group.roles)
            resource_key = self._resource_key(tenant_id, group.id)
            if resource_key in groups:
                raise RuntimeError("SCIM tenant snapshot contains duplicate groups")
            groups[resource_key] = group
        return users, groups, usernames

    def _replace_tenant_state(
        self,
        tenant_id: str,
        *,
        users: dict[tuple[str, str], ScimUser],
        groups: dict[tuple[str, str], ScimGroup],
        usernames: dict[tuple[str, str], str],
    ) -> None:
        self._users = {key: value for key, value in self._users.items() if key[0] != tenant_id}
        self._users.update(users)
        self._groups = {key: value for key, value in self._groups.items() if key[0] != tenant_id}
        self._groups.update(groups)
        self._username_index = {key: value for key, value in self._username_index.items() if key[0] != tenant_id}
        self._username_index.update(usernames)

    async def _ensure_legacy_hydrated(self, *, force: bool) -> None:
        if self._legacy_hydrated and not force:
            return
        task = self._legacy_refresh_task
        if task is None or task.done():
            task = asyncio.create_task(self._hydrate_legacy())
            self._legacy_refresh_task = task
        await asyncio.shield(task)

    async def _hydrate_legacy(self) -> None:
        persistence = self._persistence
        if persistence is None or not persistence.enabled:
            self._legacy_hydrated = True
            return
        user_loader = getattr(persistence, "load_scim_users", None)
        group_loader = getattr(persistence, "load_scim_groups", None)
        if not callable(user_loader) or not callable(group_loader):
            raise RuntimeError("legacy SCIM persistence is unavailable")
        mutation_generation = self._legacy_mutation_generation
        try:
            users = await user_loader()
            groups = await group_loader()
        except Exception as exc:
            raise RuntimeError("legacy SCIM identity load failed") from exc
        snapshot = (
            [user for user in users if user.tenant_id == ""],
            [group for group in groups if group.tenant_id == ""],
        )
        user_map, group_map, usernames = self._validated_snapshot(
            "",
            snapshot,
        )
        if mutation_generation != self._legacy_mutation_generation:
            logger.info("Discarding stale legacy SCIM hydration")
            return
        self._replace_tenant_state(
            "",
            users=user_map,
            groups=group_map,
            usernames=usernames,
        )
        self._legacy_hydrated = True

    def _note_local_mutation(self, tenant_id: str) -> None:
        if not tenant_id:
            self._legacy_mutation_generation += 1
            self._legacy_hydrated = True
            return
        self._tenant_mutation_generations[tenant_id] = self._tenant_mutation_generations.get(tenant_id, 0) + 1
        self._tenant_last_version_checks[tenant_id] = float("-inf")
        self._tenant_hydrated.add(tenant_id)

    # -- users ---------------------------------------------------------------

    async def create_user(self, user: ScimUser) -> ScimUser:
        self._validate_tenant_resource(user.tenant_id)
        await self.ensure_tenant_current(user.tenant_id)
        key = self._username_key(user.tenant_id, user.user_name)
        if key in self._username_index:
            raise ScimConflictError(f"userName '{user.user_name}' already exists")
        if not user.id:
            user.id = f"scu_{secrets.token_hex(12)}"
        if self._canonical_identity_required:
            if not user.issuer.strip():
                raise ScimValidationError("SCIM issuer is required")
            if not user.subject.strip():
                raise ScimValidationError("externalId must match the identity-provider subject")
            user.groups = []
        user.authorization_version = 1
        user.updated_at = datetime.now(timezone.utc)
        await self._persist_user(
            user,
            expected_authorization_version=None,
            previous_user=None,
        )
        self._users[self._resource_key(user.tenant_id, user.id)] = user
        self._username_index[key] = user.id
        self._note_local_mutation(user.tenant_id)
        return user

    def get_user(
        self,
        user_id: str,
        tenant_id: str = "",
    ) -> ScimUser | None:
        return self._users.get(self._resource_key(tenant_id, user_id))

    def get_user_by_username(
        self,
        user_name: str,
        tenant_id: str = "",
    ) -> ScimUser | None:
        uid = self._username_index.get(self._username_key(tenant_id, user_name))
        return self.get_user(uid, tenant_id) if uid else None

    async def replace_user(
        self,
        user_id: str,
        user: ScimUser,
        tenant_id: str = "",
    ) -> ScimUser:
        self._validate_tenant_resource(tenant_id)
        await self.ensure_tenant_current(tenant_id)
        resource_key = self._resource_key(tenant_id, user_id)
        existing = self._users.get(resource_key)
        if existing is None:
            raise ScimNotFoundError(user_id)
        if user.issuer != existing.issuer or user.subject != existing.subject:
            raise ScimValidationError("issuer and externalId are immutable identity fields")
        # userName is the stable index key; keep the index consistent on rename.
        old_username_key = self._username_key(tenant_id, existing.user_name)
        new_username_key = self._username_key(tenant_id, user.user_name)
        conflicting_id = self._username_index.get(new_username_key)
        if conflicting_id is not None and conflicting_id != user_id:
            raise ScimConflictError(f"userName '{user.user_name}' already exists")
        user.id = user_id
        user.tenant_id = tenant_id
        if self._canonical_identity_required:
            user.groups = []
        user.created_at = existing.created_at
        user.updated_at = datetime.now(timezone.utc)
        # Project-member admin routes own these additional grants. A full SCIM
        # PUT replaces IdP-managed fields but must not silently erase grants
        # committed through the tenant control plane.
        user.project_ids = list(existing.project_ids)
        user.authorization_version = existing.authorization_version + 1
        await self._persist_user(
            user,
            expected_authorization_version=existing.authorization_version,
            previous_user=existing,
        )
        if old_username_key != new_username_key:
            self._username_index.pop(old_username_key, None)
            self._username_index[new_username_key] = user_id
        self._users[resource_key] = user
        self._note_local_mutation(tenant_id)
        return user

    async def set_user_active(
        self,
        user_id: str,
        active: bool,
        tenant_id: str = "",
    ) -> ScimUser:
        """The joiner/mover/leaver switch — IdP deprovision sets active=false."""
        self._validate_tenant_resource(tenant_id)
        await self.ensure_tenant_current(tenant_id)
        resource_key = self._resource_key(tenant_id, user_id)
        user = self._users.get(resource_key)
        if user is None:
            raise ScimNotFoundError(user_id)
        if user.active is active:
            return user
        updated = replace(
            user,
            active=active,
            authorization_version=user.authorization_version + 1,
            updated_at=datetime.now(timezone.utc),
        )
        await self._persist_user(
            updated,
            expected_authorization_version=user.authorization_version,
            previous_user=user,
        )
        self._users[resource_key] = updated
        self._note_local_mutation(tenant_id)
        return updated

    async def delete_user(
        self,
        user_id: str,
        tenant_id: str = "",
    ) -> None:
        self._validate_tenant_resource(tenant_id)
        await self.ensure_tenant_current(tenant_id)
        resource_key = self._resource_key(tenant_id, user_id)
        user = self._users.get(resource_key)
        if user is None:
            raise ScimNotFoundError(user_id)
        if self._canonical_identity_required:
            deleted = replace(
                user,
                active=False,
                deleted=True,
                authorization_version=user.authorization_version + 1,
                updated_at=datetime.now(timezone.utc),
            )
            await self._persist_user(
                deleted,
                expected_authorization_version=user.authorization_version,
                previous_user=user,
            )
        elif self._persistence and self._persistence.enabled:
            await self._persistence.delete_scim_user(user_id)
        self._users.pop(resource_key, None)
        self._username_index.pop(
            self._username_key(tenant_id, user.user_name),
            None,
        )
        self._note_local_mutation(tenant_id)

    def list_users(
        self,
        user_name: str | None = None,
        start: int = 1,
        count: int = 100,
        tenant_id: str = "",
    ) -> tuple[list[ScimUser], int]:
        """List users, optionally filtered by exact userName. Returns (page, total).

        ``start`` is 1-based (SCIM startIndex). Only the ``userName eq`` filter is
        supported — the one every IdP uses to reconcile.
        """
        if user_name is not None:
            u = self.get_user_by_username(user_name, tenant_id)
            results = [u] if u else []
        else:
            results = sorted(
                (user for (owner, _), user in self._users.items() if owner == tenant_id),
                key=lambda x: x.created_at,
            )
        total = len(results)
        page = results[max(0, start - 1) : max(0, start - 1) + count]
        return page, total

    # -- groups --------------------------------------------------------------

    async def create_group(self, group: ScimGroup) -> ScimGroup:
        self._validate_tenant_resource(group.tenant_id)
        await self.ensure_tenant_current(group.tenant_id)
        if not group.id:
            group.id = f"scg_{secrets.token_hex(12)}"
        group.authorization_version = 1
        group.updated_at = datetime.now(timezone.utc)
        resource_key = self._resource_key(group.tenant_id, group.id)
        if resource_key in self._groups:
            raise ScimConflictError(f"group id '{group.id}' already exists")
        staged_groups = dict(self._groups)
        staged_groups[resource_key] = group
        user_updates = self._group_user_updates(
            group.tenant_id,
            group.id,
            existing=None,
            replacement=group,
            staged_groups=staged_groups,
        )
        await self._persist_group(
            group,
            expected_authorization_version=None,
            previous_group=None,
            user_updates=user_updates,
        )
        self._groups[resource_key] = group
        self._apply_user_updates(user_updates)
        self._note_local_mutation(group.tenant_id)
        return group

    def get_group(
        self,
        group_id: str,
        tenant_id: str = "",
    ) -> ScimGroup | None:
        return self._groups.get(self._resource_key(tenant_id, group_id))

    async def replace_group(
        self,
        group_id: str,
        group: ScimGroup,
        tenant_id: str = "",
    ) -> ScimGroup:
        self._validate_tenant_resource(tenant_id)
        await self.ensure_tenant_current(tenant_id)
        resource_key = self._resource_key(tenant_id, group_id)
        existing = self._groups.get(resource_key)
        if existing is None:
            raise ScimNotFoundError(group_id)
        group.id = group_id
        group.tenant_id = tenant_id
        group.created_at = existing.created_at
        group.updated_at = datetime.now(timezone.utc)
        group.authorization_version = existing.authorization_version + 1
        staged_groups = dict(self._groups)
        staged_groups[resource_key] = group
        user_updates = self._group_user_updates(
            tenant_id,
            group_id,
            existing=existing,
            replacement=group,
            staged_groups=staged_groups,
        )
        await self._persist_group(
            group,
            expected_authorization_version=(existing.authorization_version),
            previous_group=existing,
            user_updates=user_updates,
        )
        self._groups[resource_key] = group
        self._apply_user_updates(user_updates)
        self._note_local_mutation(tenant_id)
        return group

    async def delete_group(
        self,
        group_id: str,
        tenant_id: str = "",
    ) -> None:
        self._validate_tenant_resource(tenant_id)
        await self.ensure_tenant_current(tenant_id)
        resource_key = self._resource_key(tenant_id, group_id)
        existing = self._groups.get(resource_key)
        if existing is None:
            raise ScimNotFoundError(group_id)
        if self._canonical_identity_required:
            deleted = replace(
                existing,
                deleted=True,
                authorization_version=(existing.authorization_version + 1),
                updated_at=datetime.now(timezone.utc),
            )
            staged_groups = dict(self._groups)
            staged_groups.pop(resource_key, None)
            user_updates = self._group_user_updates(
                tenant_id,
                group_id,
                existing=existing,
                replacement=None,
                staged_groups=staged_groups,
            )
            await self._persist_group(
                deleted,
                expected_authorization_version=(existing.authorization_version),
                previous_group=existing,
                user_updates=user_updates,
            )
            self._apply_user_updates(user_updates)
        elif self._persistence and self._persistence.enabled:
            await self._persistence.delete_scim_group(group_id, tenant_id)
        self._groups.pop(resource_key, None)
        self._note_local_mutation(tenant_id)

    def list_groups(
        self,
        start: int = 1,
        count: int = 100,
        tenant_id: str = "",
    ) -> tuple[list[ScimGroup], int]:
        results = sorted(
            (group for (owner, _), group in self._groups.items() if owner == tenant_id),
            key=lambda x: x.created_at,
        )
        total = len(results)
        return results[max(0, start - 1) : max(0, start - 1) + count], total

    # -- role resolution -----------------------------------------------------

    def roles_for_user(self, user: ScimUser) -> list[str]:
        """Effective roles = the user's own roles ∪ the roles of their groups."""
        return self._roles_for_user(user, self._groups)

    def _roles_for_user(
        self,
        user: ScimUser,
        groups: dict[tuple[str, str], ScimGroup],
    ) -> list[str]:
        roles = set(user.roles)
        if not self._canonical_identity_required:
            for gid in user.groups:
                group = groups.get((user.tenant_id, gid))
                if group:
                    roles.update(group.roles)
        for (owner, _), group in groups.items():
            if owner == user.tenant_id and user.id in group.members:
                roles.update(group.roles)
        return sorted(roles)

    def groups_for_user(self, user: ScimUser) -> list[str]:
        group_ids = (
            set()
            if self._canonical_identity_required
            else set(user.groups)
        )
        for (owner, group_id), group in self._groups.items():
            if owner == user.tenant_id and user.id in group.members:
                group_ids.add(group_id)
        return sorted(group_ids)

    def _principal_for_user(
        self,
        user: ScimUser,
        groups: dict[tuple[str, str], ScimGroup] | None = None,
    ) -> Principal:
        effective_groups = self._groups if groups is None else groups
        role_names = self._roles_for_user(
            user,
            effective_groups,
        ) or [TenantRole.TENANT_MEMBER.value]
        roles = self._canonical_roles(role_names)
        projects = {
            project_id
            for project_id in user.project_ids
            if project_id.strip()
        }
        if user.project_id.strip():
            projects.add(user.project_id)
        return Principal(
            principal_id=f"scim:{user.id}",
            tenant_id=user.tenant_id,
            subject=user.subject,
            issuer=user.issuer,
            roles=roles,
            auth_method=AuthMethod.OIDC_JWT,
            membership_status=(
                MembershipStatus.ACTIVE if user.active and not user.deleted else MembershipStatus.DEPROVISIONED
            ),
            project_ids=frozenset(projects),
            authorization_version=user.authorization_version,
            email=user.primary_email or None,
        )

    def _group_user_updates(
        self,
        tenant_id: str,
        group_id: str,
        *,
        existing: ScimGroup | None,
        replacement: ScimGroup | None,
        staged_groups: dict[tuple[str, str], ScimGroup],
    ) -> list[tuple[ScimUser, Principal, int]]:
        if not self._canonical_identity_required:
            return []
        role_source = replacement or existing
        if role_source is not None:
            self._canonical_roles(role_source.roles)
        member_ids = set(existing.members if existing else ())
        member_ids.update(replacement.members if replacement else ())
        affected = [
            user
            for (owner, _), user in self._users.items()
            if owner == tenant_id and user.id in member_ids
        ]
        required_member_ids = set(replacement.members if replacement else ())
        missing = required_member_ids.difference(user.id for user in affected)
        if missing:
            raise ScimValidationError("group references unknown tenant users: " + ", ".join(sorted(missing)))
        if len(affected) > 49:
            raise ScimValidationError("a single group mutation may affect at most 49 users")
        now = datetime.now(timezone.utc)
        updates: list[tuple[ScimUser, Principal, int]] = []
        for user in affected:
            updated = replace(
                user,
                authorization_version=user.authorization_version + 1,
                updated_at=now,
            )
            updates.append(
                (
                    updated,
                    self._principal_for_user(updated, staged_groups),
                    user.authorization_version,
                )
            )
        return updates

    def _apply_user_updates(
        self,
        user_updates: list[tuple[ScimUser, Principal, int]],
    ) -> None:
        for user, _principal, _expected in user_updates:
            self._users[self._resource_key(user.tenant_id, user.id)] = user

    @classmethod
    def _canonical_roles(
        cls,
        role_names: list[str],
    ) -> frozenset[TenantRole]:
        try:
            roles = frozenset(TenantRole(role) for role in role_names)
        except ValueError as exc:
            raise ScimValidationError("roles must use canonical tenant role names") from exc
        if not roles.issubset(cls._SCIM_ROLES):
            raise ScimValidationError("SCIM cannot assign platform_admin or service roles")
        return roles

    # -- persistence helpers -------------------------------------------------

    async def _persist_user(
        self,
        user: ScimUser,
        *,
        expected_authorization_version: int | None,
        previous_user: ScimUser | None,
    ) -> None:
        if self._canonical_identity_required and not (self._persistence and self._persistence.enabled):
            raise RuntimeError("canonical SCIM persistence is disabled")
        if self._persistence and self._persistence.enabled:
            if self._canonical_identity_required:
                save = getattr(
                    self._persistence,
                    "save_scim_user_with_principal",
                    None,
                )
                if not callable(save):
                    raise RuntimeError("canonical SCIM persistence is unavailable")
                await save(
                    user,
                    self._principal_for_user(user),
                    expected_authorization_version=(expected_authorization_version),
                    previous_user=previous_user,
                )
            else:
                await self._persistence.save_scim_user(user)

    async def _persist_group(
        self,
        group: ScimGroup,
        *,
        expected_authorization_version: int | None = None,
        previous_group: ScimGroup | None = None,
        user_updates: list[tuple[ScimUser, Principal, int]] | None = None,
    ) -> None:
        if self._canonical_identity_required and not (self._persistence and self._persistence.enabled):
            raise RuntimeError("canonical SCIM persistence is disabled")
        if self._persistence and self._persistence.enabled:
            if self._canonical_identity_required:
                save = getattr(
                    self._persistence,
                    "save_scim_group_with_principals",
                    None,
                )
                if not callable(save):
                    raise RuntimeError("canonical SCIM group persistence is unavailable")
                await save(
                    group,
                    expected_authorization_version=(expected_authorization_version),
                    previous_group=previous_group,
                    user_updates=user_updates or [],
                )
            else:
                await self._persistence.save_scim_group(group)
