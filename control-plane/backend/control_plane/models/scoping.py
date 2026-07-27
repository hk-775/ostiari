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


async def org_of_gateway(db: AsyncSession, gateway_id: str) -> str:
    """The org that owns a gateway — the authoritative org for what it reports.

    Gateways post usage, payments, and approvals with no user token, so there is
    no caller org to scope by. Deriving it from the `gateways` row is the only
    trustworthy source: letting the payload name its own org would let one tenant
    write into another's ledger or approval queue. An unknown or empty gateway
    falls back to the default org so its records are still kept (the demo
    posture) rather than vanishing.
    """
    from control_plane.models.database import DEFAULT_ORG, Gateway

    if not gateway_id:
        return DEFAULT_ORG
    gw = await db.get(Gateway, gateway_id)
    return (getattr(gw, "org_id", None) or DEFAULT_ORG) if gw else DEFAULT_ORG


async def get_scoped(db: AsyncSession, model: Any, pk: Any, org: str) -> Any | None:
    """`db.get` + org check. Returns None when the row is missing OR belongs to
    another org — so a cross-org access is indistinguishable from "not found"
    (a 404), which is the correct isolation behavior."""
    obj = await db.get(model, pk)
    if obj is None or getattr(obj, "org_id", None) != org:
        return None
    return obj
