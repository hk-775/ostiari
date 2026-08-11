"""add experiment attribution to usage records

Revision ID: c5f9a2d4e6b8
Revises: b4e8f0a1c2d3
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "c5f9a2d4e6b8"
down_revision = "b4e8f0a1c2d3"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        column["name"]
        for column in inspect(op.get_bind()).get_columns("usage_records")
    }


def upgrade() -> None:
    columns = _columns()
    with op.batch_alter_table("usage_records") as batch:
        if "experiment_name" not in columns:
            batch.add_column(
                sa.Column(
                    "experiment_name",
                    sa.String(length=128),
                    nullable=False,
                    server_default="",
                )
            )
        if "experiment_variant" not in columns:
            batch.add_column(
                sa.Column(
                    "experiment_variant",
                    sa.String(length=8),
                    nullable=False,
                    server_default="",
                )
            )


def downgrade() -> None:
    columns = _columns()
    with op.batch_alter_table("usage_records") as batch:
        if "experiment_variant" in columns:
            batch.drop_column("experiment_variant")
        if "experiment_name" in columns:
            batch.drop_column("experiment_name")
