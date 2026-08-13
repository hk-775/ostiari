"""Durable tenant-scoped control-plane runtime configuration."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models.database import RuntimeStateRecord, RuntimeStateSequence


async def allocate_runtime_id(
    db: AsyncSession,
    org: str,
    namespace: str,
) -> int:
    """Atomically allocate a tenant-scoped positive integer."""
    dialect = db.get_bind().dialect.name
    insert_fn = postgresql_insert if dialect == "postgresql" else sqlite_insert
    await db.execute(
        insert_fn(RuntimeStateSequence)
        .values(org_id=org, namespace=namespace, next_value=1)
        .on_conflict_do_nothing(
            index_elements=[
                RuntimeStateSequence.org_id,
                RuntimeStateSequence.namespace,
            ]
        )
    )
    next_value = (
        await db.execute(
            update(RuntimeStateSequence)
            .where(
                RuntimeStateSequence.org_id == org,
                RuntimeStateSequence.namespace == namespace,
            )
            .values(next_value=RuntimeStateSequence.next_value + 1)
            .returning(RuntimeStateSequence.next_value)
        )
    ).scalar_one()
    return int(next_value) - 1


async def ensure_runtime_sequence(
    db: AsyncSession,
    org: str,
    namespace: str,
    minimum_next_value: int,
) -> None:
    """Advance an allocator past imported records without moving it backwards."""
    dialect = db.get_bind().dialect.name
    insert_fn = postgresql_insert if dialect == "postgresql" else sqlite_insert
    await db.execute(
        insert_fn(RuntimeStateSequence)
        .values(
            org_id=org,
            namespace=namespace,
            next_value=minimum_next_value,
        )
        .on_conflict_do_nothing(
            index_elements=[
                RuntimeStateSequence.org_id,
                RuntimeStateSequence.namespace,
            ]
        )
    )
    await db.execute(
        update(RuntimeStateSequence)
        .where(
            RuntimeStateSequence.org_id == org,
            RuntimeStateSequence.namespace == namespace,
        )
        .values(
            next_value=case(
                (
                    RuntimeStateSequence.next_value < minimum_next_value,
                    minimum_next_value,
                ),
                else_=RuntimeStateSequence.next_value,
            )
        )
    )


async def put_runtime_state(
    db: AsyncSession,
    org: str,
    namespace: str,
    item_key: str,
    value: dict[str, Any],
) -> None:
    """Insert or replace one configuration item without creating duplicates."""
    values = {
        "org_id": org,
        "namespace": namespace,
        "item_key": item_key,
        "value": value,
        "updated_at": datetime.now(timezone.utc),
    }
    dialect = db.get_bind().dialect.name
    insert_fn = postgresql_insert if dialect == "postgresql" else sqlite_insert
    statement = insert_fn(RuntimeStateRecord).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[
            RuntimeStateRecord.org_id,
            RuntimeStateRecord.namespace,
            RuntimeStateRecord.item_key,
        ],
        set_={
            "value": statement.excluded.value,
            "updated_at": statement.excluded.updated_at,
        },
    )
    await db.execute(statement)


async def delete_runtime_state(
    db: AsyncSession,
    org: str,
    namespace: str,
    item_key: str,
) -> None:
    await db.execute(
        delete(RuntimeStateRecord).where(
            RuntimeStateRecord.org_id == org,
            RuntimeStateRecord.namespace == namespace,
            RuntimeStateRecord.item_key == item_key,
        )
    )


async def clear_runtime_namespace(
    db: AsyncSession,
    org: str,
    namespace: str,
) -> None:
    await db.execute(
        delete(RuntimeStateRecord).where(
            RuntimeStateRecord.org_id == org,
            RuntimeStateRecord.namespace == namespace,
        )
    )


async def load_runtime_namespace(
    db: AsyncSession,
    org: str,
    namespace: str,
) -> dict[str, dict[str, Any]]:
    rows = (
        await db.execute(
            select(RuntimeStateRecord).where(
                RuntimeStateRecord.org_id == org,
                RuntimeStateRecord.namespace == namespace,
            )
        )
    ).scalars()
    return {row.item_key: dict(row.value or {}) for row in rows}


async def load_all_runtime_state(
    db: AsyncSession,
) -> list[RuntimeStateRecord]:
    return list(
        (
            await db.execute(
                select(RuntimeStateRecord).order_by(
                    RuntimeStateRecord.org_id,
                    RuntimeStateRecord.namespace,
                    RuntimeStateRecord.item_key,
                )
            )
        ).scalars()
    )
