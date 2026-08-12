"""Add durable provider credential and endpoint routes.

Revision ID: a2c4e6f8b0d2
Revises: f9a1b3c5d7e9
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "a2c4e6f8b0d2"
down_revision = "f9a1b3c5d7e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_routes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("route_id", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("endpoint", sa.String(length=512), nullable=False),
        sa.Column("auth_type", sa.String(length=32), nullable=False),
        sa.Column("private_config_encrypted", sa.Text(), nullable=False),
        sa.Column("region", sa.String(length=64), nullable=False),
        sa.Column("allowed_models", sa.JSON(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("capacity_group", sa.String(length=128), nullable=False),
        sa.Column("capacity_limit", sa.Integer(), nullable=False),
        sa.Column("connect_timeout", sa.Float(), nullable=False),
        sa.Column("read_timeout", sa.Float(), nullable=False),
        sa.Column("max_connections", sa.Integer(), nullable=False),
        sa.Column("max_connections_per_host", sa.Integer(), nullable=False),
        sa.Column("keepalive_timeout", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "weight > 0",
            name="ck_provider_routes_positive_weight",
        ),
        sa.CheckConstraint(
            "priority >= 0 AND max_concurrency > 0 AND capacity_limit >= 0",
            name="ck_provider_routes_capacity",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "route_id",
            name="uq_provider_routes_org_route_id",
        ),
    )
    op.create_index(
        op.f("ix_provider_routes_org_id"),
        "provider_routes",
        ["org_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_provider_routes_provider"),
        "provider_routes",
        ["provider"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_provider_routes_provider"),
        table_name="provider_routes",
    )
    op.drop_index(
        op.f("ix_provider_routes_org_id"),
        table_name="provider_routes",
    )
    op.drop_table("provider_routes")
