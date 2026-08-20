"""Authoritative tenant-qualified project ownership resolution."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
from typing import TYPE_CHECKING, Protocol

from src.gateway.models import Project
from src.gateway.persistence import (
    PersistenceConflictError,
    tenant_project_partition_key,
    tenant_project_sort_key,
)

if TYPE_CHECKING:
    from src.gateway.persistence import DynamoPersistence

logger = logging.getLogger(__name__)


class ProjectRepositoryError(RuntimeError):
    """Base error for authoritative project-store failures."""


class ProjectStoreUnavailable(ProjectRepositoryError):
    """Tenant project ownership could not be established safely."""


class ProjectConfigConflict(ProjectRepositoryError):
    """A project configuration changed before a conditional update."""


class ProjectResolver(Protocol):
    """Resolve a project only inside an explicit tenant namespace."""

    async def resolve(self, tenant_id: str, project_id: str) -> Project | None:
        ...


class ProjectConfigStore(Protocol):
    """Conditionally persist one canonical tenant project configuration."""

    async def update(
        self,
        project: Project,
        *,
        expected_revision: int,
    ) -> Project: ...


class DynamoProjectRepository:
    """Strongly consistent tenant/project lookup for authorization decisions."""

    def __init__(self, persistence: DynamoPersistence) -> None:
        self._persistence = persistence

    @property
    def enabled(self) -> bool:
        return self._persistence.enabled

    async def resolve(self, tenant_id: str, project_id: str) -> Project | None:
        if not self.enabled:
            raise ProjectStoreUnavailable(
                "tenant project persistence is disabled"
            )
        if not tenant_id.strip() or not project_id.strip():
            return None

        expected_key = {
            "PK": tenant_project_partition_key(tenant_id),
            "SK": tenant_project_sort_key(project_id),
        }

        def _get() -> dict | None:
            response = self._persistence._get_table().get_item(
                Key=expected_key,
                ConsistentRead=True,
            )
            item = response.get("Item")
            if item is None:
                return None
            if not isinstance(item, dict):
                raise ValueError("DynamoDB returned a malformed project item")
            return item

        try:
            item = await asyncio.to_thread(_get)
            if item is None:
                return None
            project = self._persistence.deserialize_project(
                self._persistence._convert_decimals_to_native(item)
            )
        except Exception as exc:
            logger.error(
                "Tenant project read failed tenant=%s project=%s",
                tenant_id,
                project_id,
                exc_info=True,
            )
            raise ProjectStoreUnavailable(
                "tenant project read failed"
            ) from exc

        if (
            item.get("entity_type") != "project"
            or item.get("PK") != expected_key["PK"]
            or item.get("SK") != expected_key["SK"]
            or project.tenant_id != tenant_id
            or project.project_id != project_id
        ):
            logger.error(
                "Tenant project row does not match its authoritative key "
                "tenant=%s project=%s",
                tenant_id,
                project_id,
            )
            raise ProjectStoreUnavailable(
                "tenant project row does not match its key"
            )
        return project

    async def update(
        self,
        project: Project,
        *,
        expected_revision: int,
    ) -> Project:
        if (
            not self.enabled
            or project.tenant_id is None
            or not project.tenant_id.strip()
        ):
            raise ProjectStoreUnavailable(
                "tenant project persistence is disabled"
            )
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError(
                "expected project revision must be a non-negative integer"
            )
        try:
            revision = await self._persistence.save_project(
                project,
                expected_revision=expected_revision,
            )
        except PersistenceConflictError as exc:
            raise ProjectConfigConflict(
                "project configuration changed concurrently"
            ) from exc
        except Exception as exc:
            logger.error(
                "Tenant project update failed tenant=%s project=%s",
                project.tenant_id,
                project.project_id,
                exc_info=True,
            )
            raise ProjectStoreUnavailable(
                "tenant project update failed"
            ) from exc
        if revision != expected_revision + 1:
            raise ProjectStoreUnavailable(
                "tenant project update returned an invalid revision"
            )
        return replace(project, revision=revision)
