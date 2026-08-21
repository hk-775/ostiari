"""Bind gateways to immutable workload OIDC identities.

Revision ID: a4c6e8f0b2d4
Revises: b3d5f7a9c1e3
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "a4c6e8f0b2d4"
down_revision = "b3d5f7a9c1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNIQUE_NAME = "uq_gateways_workload_identity"


def _columns() -> set[str]:
    return {
        column["name"]
        for column in inspect(op.get_bind()).get_columns("gateways")
    }


def _unique_constraints() -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(op.get_bind()).get_unique_constraints("gateways")
        if constraint.get("name")
    }


def upgrade() -> None:
    columns = _columns()
    with op.batch_alter_table("gateways") as batch:
        if "workload_issuer" not in columns:
            batch.add_column(
                sa.Column("workload_issuer", sa.String(length=512), nullable=True)
            )
        if "workload_subject" not in columns:
            batch.add_column(
                sa.Column("workload_subject", sa.String(length=512), nullable=True)
            )

    if _UNIQUE_NAME not in _unique_constraints():
        with op.batch_alter_table("gateways") as batch:
            batch.create_unique_constraint(
                _UNIQUE_NAME,
                ["workload_issuer", "workload_subject"],
            )


def downgrade() -> None:
    if _UNIQUE_NAME in _unique_constraints():
        with op.batch_alter_table("gateways") as batch:
            batch.drop_constraint(_UNIQUE_NAME, type_="unique")

    columns = _columns()
    with op.batch_alter_table("gateways") as batch:
        for name in ("workload_subject", "workload_issuer"):
            if name in columns:
                batch.drop_column(name)
