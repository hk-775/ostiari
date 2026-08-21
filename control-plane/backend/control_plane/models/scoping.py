"""Tenant (org) query-scoping helpers.

Every tenant-scoped table carries an `org_id`. These helpers make scoping a
select, stamping a new row, and doing an org-checked primary-key fetch uniform
across routers. Kept dependency-light (no FastAPI import) so they're trivially
unit-testable.

The org value comes from `auth.dependencies.get_current_org` at the route layer
and defaults to "default" (the single-org dev/demo tenant), so unscoped/demo
behavior is unchanged.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select


def scoped(stmt: Select, model: Any, org: str) -> Select:
    """Append `WHERE model.org_id == org` to a select."""
    return stmt.where(model.org_id == org)


def stamp(obj: Any, org: str) -> Any:
    """Set `org_id` on a new model instance before db.add (no-op if already set)."""
    if getattr(obj, "org_id", None) in (None, ""):
        obj.org_id = org
    return obj


async def get_scoped(db: AsyncSession, model: Any, pk: Any, org: str) -> Any | None:
    """`db.get` + org check. Returns None when the row is missing OR belongs to
    another org — so a cross-org access is indistinguishable from "not found"
    (a 404), which is the correct isolation behavior."""
    primary_keys = [column.key for column in inspect(model).primary_key]
    if "org_id" in primary_keys and len(primary_keys) == 2:
        identity = {
            "org_id": org,
            next(key for key in primary_keys if key != "org_id"): pk,
        }
        obj = await db.get(model, identity)
    else:
        obj = await db.get(model, pk)
    if obj is None or getattr(obj, "org_id", None) != org:
        return None
    return obj


async def get_gateway(
    db: AsyncSession,
    gateway_id: str,
    org: str,
) -> Any | None:
    """Fetch a gateway by its tenant-qualified natural identity."""
    from control_plane.models.database import Gateway

    return await db.get(
        Gateway,
        {"org_id": org, "id": gateway_id},
    )
