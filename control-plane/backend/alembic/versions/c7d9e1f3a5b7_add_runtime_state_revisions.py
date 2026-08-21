"""Add monotonic runtime-state namespace revisions.

Revision ID: c7d9e1f3a5b7
Revises: a4c6e8f0b2d4
"""

import sqlalchemy as sa
from alembic import op

revision = "c7d9e1f3a5b7"
down_revision = "a4c6e8f0b2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_state_revisions",
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "org_id",
            "namespace",
            name=op.f("pk_runtime_state_revisions"),
        ),
    )
    op.execute(
        """
        INSERT INTO runtime_state_revisions
            (org_id, namespace, revision, updated_at)
        SELECT org_id, namespace, 1, MAX(updated_at)
        FROM runtime_state_records
        GROUP BY org_id, namespace
        """
    )


def downgrade() -> None:
    op.drop_table("runtime_state_revisions")
