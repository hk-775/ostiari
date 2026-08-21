"""Tenant-qualified datasource repositories."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Protocol

from src.gateway.persistence import (
    DynamoPersistence,
    PersistenceConflictError,
    PersistenceQuotaExceededError,
)

from .models import AthenaDatasource


class DatasourceStoreUnavailable(RuntimeError):
    """Raised when datasource authority cannot be read or changed safely."""


class DatasourceConflictError(RuntimeError):
    """Raised when a datasource CAS write loses a concurrent update."""


class DatasourceCursorError(ValueError):
    """Raised when a datasource page cursor is malformed or out of scope."""


class DatasourceQuotaExceededError(RuntimeError):
    """Raised when a tenant reaches its datasource cardinality limit."""


@dataclass(frozen=True)
class DatasourcePage:
    """One bounded datasource page and its opaque continuation cursor."""

    items: tuple[AthenaDatasource, ...]
    next_cursor: str | None = None


_MAX_PAGE_SIZE = 100


def _sort_key(project_id: str, datasource_id: str) -> str:
    return f"DATASOURCE#{project_id}#{datasource_id}"


def _encode_cursor(sort_key: str | None) -> str | None:
    if sort_key is None:
        return None
    payload = json.dumps(
        {"v": 1, "after": sort_key},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str | None,
    *,
    project_id: str | None,
) -> str | None:
    if cursor is None:
        return None
    if (
        not isinstance(cursor, str)
        or not cursor
        or len(cursor) > 1024
        or any(character.isspace() for character in cursor)
    ):
        raise DatasourceCursorError("datasource cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(
            base64.b64decode(
                cursor + padding,
                altchars=b"-_",
                validate=True,
            )
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise DatasourceCursorError(
            "datasource cursor is invalid"
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"v", "after"}
        or payload.get("v") != 1
        or not isinstance(payload.get("after"), str)
    ):
        raise DatasourceCursorError("datasource cursor is invalid")
    after = payload["after"]
    prefix = (
        f"DATASOURCE#{project_id}#"
        if project_id is not None
        else "DATASOURCE#"
    )
    if not after.startswith(prefix):
        raise DatasourceCursorError(
            "datasource cursor does not match the requested scope"
        )
    return after


def _page_size(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= _MAX_PAGE_SIZE
    ):
        raise ValueError(
            f"datasource page size must be between 1 and {_MAX_PAGE_SIZE}"
        )
    return value


class DatasourceRepository(Protocol):
    async def get(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
    ) -> AthenaDatasource | None: ...

    async def list(
        self,
        tenant_id: str,
        *,
        project_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> DatasourcePage: ...

    async def save(
        self,
        datasource: AthenaDatasource,
        *,
        expected_revision: int,
    ) -> AthenaDatasource: ...

    async def delete(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
        *,
        expected_revision: int,
    ) -> None: ...


def _updated_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expected_revision(value: object, *, delete: bool = False) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < (1 if delete else 0)
    ):
        qualifier = "positive" if delete else "non-negative"
        raise ValueError(
            f"expected_revision must be a {qualifier} integer"
        )
    return value


class DynamoDatasourceRepository:
    """Strongly consistent datasource authority in the canonical table."""

    def __init__(
        self,
        persistence: DynamoPersistence,
        *,
        max_datasources_per_tenant: int = 500,
    ) -> None:
        if (
            isinstance(max_datasources_per_tenant, bool)
            or not isinstance(max_datasources_per_tenant, int)
            or not 1 <= max_datasources_per_tenant <= 10_000
        ):
            raise ValueError(
                "max_datasources_per_tenant must be between 1 and 10000"
            )
        self._persistence = persistence
        self._max_datasources_per_tenant = max_datasources_per_tenant

    @staticmethod
    def _deserialize(value: dict) -> AthenaDatasource:
        config = {
            key: item
            for key, item in value.items()
            if key
            not in {
                "tenant_id",
                "project_id",
                "datasource_id",
                "revision",
                "created_at",
                "updated_at",
            }
        }
        return AthenaDatasource.from_mapping(
            config,
            tenant_id=value["tenant_id"],
            project_id=value["project_id"],
            datasource_id=value["datasource_id"],
            revision=value["revision"],
            created_at=value["created_at"],
            updated_at=value["updated_at"],
        )

    async def get(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
    ) -> AthenaDatasource | None:
        try:
            value = await self._persistence.get_tenant_datasource(
                tenant_id,
                project_id,
                datasource_id,
            )
        except Exception as exc:
            raise DatasourceStoreUnavailable(
                "datasource authority is unavailable"
            ) from exc
        if value is None:
            return None
        try:
            datasource = self._deserialize(value)
            if (
                datasource.tenant_id != tenant_id
                or datasource.project_id != project_id
                or datasource.datasource_id != datasource_id
            ):
                raise ValueError(
                    "datasource authority returned a mismatched owner"
                )
            return datasource
        except (KeyError, TypeError, ValueError) as exc:
            raise DatasourceStoreUnavailable(
                "datasource authority returned malformed state"
            ) from exc

    async def list(
        self,
        tenant_id: str,
        *,
        project_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> DatasourcePage:
        limit = _page_size(limit)
        after = _decode_cursor(cursor, project_id=project_id)
        try:
            values, next_key = (
                await self._persistence.list_tenant_datasources(
                    tenant_id,
                    project_id=project_id,
                    limit=limit,
                    exclusive_start_key=after,
                )
            )
            datasources = [self._deserialize(value) for value in values]
            if any(
                datasource.tenant_id != tenant_id
                or (
                    project_id is not None
                    and datasource.project_id != project_id
                )
                for datasource in datasources
            ):
                raise ValueError(
                    "datasource authority returned a mismatched owner"
                )
        except DatasourceCursorError:
            raise
        except Exception as exc:
            raise DatasourceStoreUnavailable(
                "datasource authority is unavailable"
            ) from exc
        return DatasourcePage(
            items=tuple(
                sorted(
                    datasources,
                    key=lambda item: (
                        item.project_id,
                        item.datasource_id,
                    ),
                )
            ),
            next_cursor=_encode_cursor(next_key),
        )

    async def save(
        self,
        datasource: AthenaDatasource,
        *,
        expected_revision: int,
    ) -> AthenaDatasource:
        expected_revision = _expected_revision(expected_revision)
        timestamp = _updated_timestamp()
        candidate = replace(
            datasource,
            revision=expected_revision,
            created_at=datasource.created_at or timestamp,
            updated_at=timestamp,
        )
        document = candidate.to_dict()
        for key in (
            "tenant_id",
            "project_id",
            "datasource_id",
            "revision",
        ):
            document.pop(key, None)
        try:
            revision = await self._persistence.save_tenant_datasource(
                candidate.tenant_id,
                candidate.project_id,
                candidate.datasource_id,
                document,
                expected_revision=expected_revision,
                max_datasources=self._max_datasources_per_tenant,
            )
        except PersistenceQuotaExceededError as exc:
            raise DatasourceQuotaExceededError(str(exc)) from exc
        except PersistenceConflictError as exc:
            raise DatasourceConflictError(str(exc)) from exc
        except Exception as exc:
            raise DatasourceStoreUnavailable(
                "datasource authority is unavailable"
            ) from exc
        return replace(candidate, revision=revision)

    async def delete(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
        *,
        expected_revision: int,
    ) -> None:
        expected_revision = _expected_revision(
            expected_revision,
            delete=True,
        )
        try:
            await self._persistence.delete_tenant_datasource(
                tenant_id,
                project_id,
                datasource_id,
                expected_revision=expected_revision,
                max_datasources=self._max_datasources_per_tenant,
            )
        except PersistenceConflictError as exc:
            raise DatasourceConflictError(str(exc)) from exc
        except Exception as exc:
            raise DatasourceStoreUnavailable(
                "datasource authority is unavailable"
            ) from exc


class InMemoryDatasourceRepository:
    """CAS-compatible repository for local development and focused tests."""

    def __init__(
        self,
        *,
        max_datasources_per_tenant: int = 500,
    ) -> None:
        if (
            isinstance(max_datasources_per_tenant, bool)
            or not isinstance(max_datasources_per_tenant, int)
            or not 1 <= max_datasources_per_tenant <= 10_000
        ):
            raise ValueError(
                "max_datasources_per_tenant must be between 1 and 10000"
            )
        self._items: dict[tuple[str, str, str], AthenaDatasource] = {}
        self._lock = asyncio.Lock()
        self._max_datasources_per_tenant = max_datasources_per_tenant

    @staticmethod
    def _key(
        tenant_id: str,
        project_id: str,
        datasource_id: str,
    ) -> tuple[str, str, str]:
        return tenant_id, project_id, datasource_id

    async def get(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
    ) -> AthenaDatasource | None:
        return self._items.get(
            self._key(tenant_id, project_id, datasource_id)
        )

    async def list(
        self,
        tenant_id: str,
        *,
        project_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> DatasourcePage:
        limit = _page_size(limit)
        after = _decode_cursor(cursor, project_id=project_id)
        items = sorted(
            (
                datasource
                for datasource in self._items.values()
                if datasource.tenant_id == tenant_id
                and (
                    project_id is None
                    or datasource.project_id == project_id
                )
            ),
            key=lambda item: (
                item.project_id,
                item.datasource_id,
            ),
        )
        if after is not None:
            items = [
                item
                for item in items
                if _sort_key(item.project_id, item.datasource_id) > after
            ]
        selected = items[:limit]
        next_key = (
            _sort_key(
                selected[-1].project_id,
                selected[-1].datasource_id,
            )
            if len(items) > limit and selected
            else None
        )
        return DatasourcePage(
            items=tuple(selected),
            next_cursor=_encode_cursor(next_key),
        )

    async def save(
        self,
        datasource: AthenaDatasource,
        *,
        expected_revision: int,
    ) -> AthenaDatasource:
        expected_revision = _expected_revision(expected_revision)
        async with self._lock:
            key = self._key(
                datasource.tenant_id,
                datasource.project_id,
                datasource.datasource_id,
            )
            current = self._items.get(key)
            current_revision = current.revision if current is not None else 0
            if current_revision != expected_revision:
                raise DatasourceConflictError(
                    "datasource changed concurrently"
                )
            if current is None and sum(
                item.tenant_id == datasource.tenant_id
                for item in self._items.values()
            ) >= self._max_datasources_per_tenant:
                raise DatasourceQuotaExceededError(
                    "tenant datasource quota exceeded"
                )
            timestamp = _updated_timestamp()
            saved = replace(
                datasource,
                revision=expected_revision + 1,
                created_at=(
                    current.created_at
                    if current is not None
                    else datasource.created_at or timestamp
                ),
                updated_at=timestamp,
            )
            self._items[key] = saved
            return saved

    async def delete(
        self,
        tenant_id: str,
        project_id: str,
        datasource_id: str,
        *,
        expected_revision: int,
    ) -> None:
        expected_revision = _expected_revision(
            expected_revision,
            delete=True,
        )
        async with self._lock:
            key = self._key(tenant_id, project_id, datasource_id)
            current = self._items.get(key)
            if current is None or current.revision != expected_revision:
                raise DatasourceConflictError(
                    "datasource changed concurrently or no longer exists"
                )
            del self._items[key]
