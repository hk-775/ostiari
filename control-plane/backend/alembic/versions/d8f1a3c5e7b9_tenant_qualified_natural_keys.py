"""Qualify reusable natural identifiers by tenant.

Revision ID: d8f1a3c5e7b9
Revises: c7d9e1f3a5b7
Create Date: 2026-08-21
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "d8f1a3c5e7b9"
down_revision = "c7d9e1f3a5b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_ORG = "default"
_AUDIT_HEAD = "global"
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
_GATEWAY_CHILDREN = (
    "tools",
    "policies",
    "mcp_servers",
    "usage_records",
    "a2a_agents",
)


def _inspector() -> sa.Inspector:
    return inspect(op.get_bind())


def _primary_key_name(table: str) -> str:
    return _inspector().get_pk_constraint(table).get("name") or f"pk_{table}"


def _gateway_fk_name(table: str) -> str:
    for constraint in _inspector().get_foreign_keys(table):
        if (
            constraint["referred_table"] == "gateways"
            and constraint["constrained_columns"] == ["gateway_id"]
        ):
            return (
                constraint.get("name")
                or f"fk_{table}_gateway_id_gateways"
            )
    raise RuntimeError(f"{table} has no legacy gateway foreign key")


def _unique_name(table: str, columns: tuple[str, ...]) -> str:
    for constraint in _inspector().get_unique_constraints(table):
        if tuple(constraint["column_names"]) == columns:
            return constraint.get("name") or f"uq_{table}_{columns[0]}"
    raise RuntimeError(
        f"{table} has no unique constraint for {', '.join(columns)}"
    )


def _backfill_org(table: str) -> None:
    op.execute(
        sa.text(
            f"UPDATE {table} SET org_id=:org "
            "WHERE org_id IS NULL OR org_id=''"
        ).bindparams(org=_DEFAULT_ORG)
    )


def _align_child_org_with_gateway(table: str) -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            f"UPDATE {table} SET org_id=("
            "SELECT COALESCE(g.org_id, :org) FROM gateways g "
            f"WHERE g.id={table}.gateway_id"
            ") WHERE EXISTS ("
            f"SELECT 1 FROM gateways g WHERE g.id={table}.gateway_id"
            ")"
        ),
        {"org": _DEFAULT_ORG},
    )
    _backfill_org(table)


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return dict(value)


def _timestamp_string(value: Any) -> str:
    timestamp = value
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        )
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
    return timestamp.replace(microsecond=0).isoformat()


def _audit_hash(previous: str, content: str) -> str:
    return hashlib.sha256(
        (previous + "|" + content).encode()
    ).hexdigest()


def _rebuild_audit_chains(*, tenant_scoped: bool) -> dict[str, str]:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, org_id, actor, action, resource_type, resource_id, "
            "details, timestamp FROM audit_logs ORDER BY id"
        )
    ).mappings()
    heads: dict[str, str] = {}
    for row in rows:
        org = str(row["org_id"] or _DEFAULT_ORG)
        chain = org if tenant_scoped else _AUDIT_HEAD
        previous = heads.get(chain, "")
        content_data = {
            "actor": row["actor"],
            "action": row["action"],
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "details": _json_value(row["details"]),
            "timestamp": _timestamp_string(row["timestamp"]),
        }
        if tenant_scoped:
            content_data["org_id"] = org
        content = json.dumps(
            content_data,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        entry_hash = _audit_hash(previous, content)
        bind.execute(
            sa.text(
                "UPDATE audit_logs SET prev_hash=:previous, "
                "entry_hash=:entry_hash WHERE id=:id"
            ),
            {
                "previous": previous,
                "entry_hash": entry_hash,
                "id": row["id"],
            },
        )
        heads[chain] = entry_hash
    return heads


def _assert_downgrade_has_no_collisions() -> None:
    bind = op.get_bind()
    for table in ("usage_records", "payment_records"):
        duplicate = bind.execute(
            sa.text(
                f"SELECT gateway_id, event_id FROM {table} "
                "WHERE event_id IS NOT NULL "
                "GROUP BY gateway_id, event_id "
                "HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).first()
        if duplicate is not None:
            raise RuntimeError(
                f"Cannot downgrade tenant-qualified {table} idempotency key: "
                f"value {(duplicate[0], duplicate[1])!r} exists in multiple tenants"
            )

    checks = (
        ("gateways", "id"),
        ("wallets", "agent_id"),
        ("users", "email"),
        ("policies", "name"),
    )
    for table, column in checks:
        duplicate = bind.execute(
            sa.text(
                f"SELECT {column} FROM {table} "
                f"GROUP BY {column} HAVING COUNT(*) > 1 LIMIT 1"
            )
        ).scalar()
        if duplicate is not None:
            raise RuntimeError(
                f"Cannot downgrade tenant-qualified {table}.{column}: "
                f"value {duplicate!r} exists in multiple tenants"
            )


def upgrade() -> None:
    _backfill_org("gateways")
    with op.batch_alter_table(
        "gateways",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.alter_column(
            "org_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )

    for table in _GATEWAY_CHILDREN:
        _align_child_org_with_gateway(table)
        legacy_fk = _gateway_fk_name(table)
        with op.batch_alter_table(
            table,
            naming_convention=_NAMING_CONVENTION,
        ) as batch:
            batch.alter_column(
                "org_id",
                existing_type=sa.String(length=64),
                nullable=False,
            )
            batch.drop_constraint(legacy_fk, type_="foreignkey")

    gateway_pk = _primary_key_name("gateways")
    with op.batch_alter_table(
        "gateways",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(gateway_pk, type_="primary")
        batch.create_primary_key(
            "pk_gateways",
            ["org_id", "id"],
        )

    for table in _GATEWAY_CHILDREN:
        with op.batch_alter_table(
            table,
            naming_convention=_NAMING_CONVENTION,
        ) as batch:
            batch.create_foreign_key(
                f"fk_{table}_gateway",
                "gateways",
                ["org_id", "gateway_id"],
                ["org_id", "id"],
            )

    _backfill_org("wallets")
    wallet_pk = _primary_key_name("wallets")
    with op.batch_alter_table(
        "wallets",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.alter_column(
            "org_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch.drop_constraint(wallet_pk, type_="primary")
        batch.create_primary_key(
            "pk_wallets",
            ["org_id", "agent_id"],
        )

    _backfill_org("users")
    with op.batch_alter_table(
        "users",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.alter_column(
            "org_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch.drop_index("ix_users_email")
        batch.create_index("ix_users_email", ["email"], unique=False)
        batch.create_unique_constraint(
            "uq_users_org_email",
            ["org_id", "email"],
        )

    policy_unique = _unique_name("policies", ("name",))
    with op.batch_alter_table(
        "policies",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(policy_unique, type_="unique")
        batch.create_unique_constraint(
            "uq_policies_org_name",
            ["org_id", "name"],
        )

    usage_unique = _unique_name(
        "usage_records",
        ("gateway_id", "event_id"),
    )
    with op.batch_alter_table(
        "usage_records",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(usage_unique, type_="unique")
        batch.create_unique_constraint(
            "uq_usage_records_org_gateway_event",
            ["org_id", "gateway_id", "event_id"],
        )

    _backfill_org("payment_records")
    payment_unique = _unique_name(
        "payment_records",
        ("gateway_id", "event_id"),
    )
    with op.batch_alter_table(
        "payment_records",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.alter_column(
            "org_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch.drop_constraint(payment_unique, type_="unique")
        batch.create_unique_constraint(
            "uq_payment_records_org_gateway_event",
            ["org_id", "gateway_id", "event_id"],
        )

    for table in (
        "audit_logs",
        "reconciliation_records",
    ):
        _backfill_org(table)
        with op.batch_alter_table(
            table,
            naming_convention=_NAMING_CONVENTION,
        ) as batch:
            batch.alter_column(
                "org_id",
                existing_type=sa.String(length=64),
                nullable=False,
            )

    heads = _rebuild_audit_chains(tenant_scoped=True)
    op.drop_table("audit_chain_heads")
    op.create_table(
        "audit_chain_heads",
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "org_id",
            "name",
            name="pk_audit_chain_heads",
        ),
    )
    bind = op.get_bind()
    orgs = {
        str(org)
        for org in bind.execute(
            sa.text("SELECT id FROM organizations")
        ).scalars()
    }
    orgs.update(heads)
    now = datetime.now(timezone.utc)
    for org in sorted(orgs):
        bind.execute(
            sa.text(
                "INSERT INTO audit_chain_heads "
                "(org_id, name, entry_hash, updated_at) "
                "VALUES (:org, :name, :entry_hash, :updated_at)"
            ),
            {
                "org": org,
                "name": _AUDIT_HEAD,
                "entry_hash": heads.get(org, ""),
                "updated_at": now,
            },
        )


def downgrade() -> None:
    _assert_downgrade_has_no_collisions()

    heads = _rebuild_audit_chains(tenant_scoped=False)
    op.drop_table("audit_chain_heads")
    op.create_table(
        "audit_chain_heads",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO audit_chain_heads "
            "(name, entry_hash, updated_at) "
            "VALUES (:name, :entry_hash, :updated_at)"
        ),
        {
            "name": _AUDIT_HEAD,
            "entry_hash": heads.get(_AUDIT_HEAD, ""),
            "updated_at": datetime.now(timezone.utc),
        },
    )

    payment_unique = _unique_name(
        "payment_records",
        ("org_id", "gateway_id", "event_id"),
    )
    with op.batch_alter_table(
        "payment_records",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(payment_unique, type_="unique")
        batch.create_unique_constraint(
            "uq_payment_records_gateway_event",
            ["gateway_id", "event_id"],
        )

    usage_unique = _unique_name(
        "usage_records",
        ("org_id", "gateway_id", "event_id"),
    )
    with op.batch_alter_table(
        "usage_records",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(usage_unique, type_="unique")
        batch.create_unique_constraint(
            "uq_usage_records_gateway_event",
            ["gateway_id", "event_id"],
        )

    policy_unique = _unique_name(
        "policies",
        ("org_id", "name"),
    )
    with op.batch_alter_table(
        "policies",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(policy_unique, type_="unique")
        batch.create_unique_constraint(
            "uq_policies_name",
            ["name"],
        )

    with op.batch_alter_table(
        "users",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(
            "uq_users_org_email",
            type_="unique",
        )
        batch.drop_index("ix_users_email")
        batch.create_index("ix_users_email", ["email"], unique=True)

    for table in _GATEWAY_CHILDREN:
        composite_fk = next(
            constraint["name"]
            for constraint in _inspector().get_foreign_keys(table)
            if constraint["referred_table"] == "gateways"
            and constraint["constrained_columns"] == [
                "org_id",
                "gateway_id",
            ]
        )
        with op.batch_alter_table(
            table,
            naming_convention=_NAMING_CONVENTION,
        ) as batch:
            batch.drop_constraint(composite_fk, type_="foreignkey")

    gateway_pk = _primary_key_name("gateways")
    with op.batch_alter_table(
        "gateways",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(gateway_pk, type_="primary")
        batch.create_primary_key("pk_gateways", ["id"])

    for table in _GATEWAY_CHILDREN:
        with op.batch_alter_table(
            table,
            naming_convention=_NAMING_CONVENTION,
        ) as batch:
            batch.create_foreign_key(
                f"fk_{table}_gateway_id_gateways",
                "gateways",
                ["gateway_id"],
                ["id"],
            )

    wallet_pk = _primary_key_name("wallets")
    with op.batch_alter_table(
        "wallets",
        naming_convention=_NAMING_CONVENTION,
    ) as batch:
        batch.drop_constraint(wallet_pk, type_="primary")
        batch.create_primary_key("pk_wallets", ["agent_id"])
