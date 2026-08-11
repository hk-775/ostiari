"""Add persistent metadata for isolated Sandbox code runs.

Revision ID: d7e9f1a3b5c7
Revises: c5f9a2d4e6b8
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "d7e9f1a3b5c7"
down_revision = "c5f9a2d4e6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("gateway_id", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=20), nullable=False),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column("source_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("timeout_ms", sa.Integer(), nullable=False),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False),
        sa.Column("max_output_bytes", sa.Integer(), nullable=False),
        sa.Column("max_tool_payload_bytes", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("output_bytes", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sandbox_runs_org_id", "sandbox_runs", ["org_id"])
    op.create_index("ix_sandbox_runs_gateway_id", "sandbox_runs", ["gateway_id"])
    op.create_index("ix_sandbox_runs_status", "sandbox_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_sandbox_runs_status", table_name="sandbox_runs")
    op.drop_index("ix_sandbox_runs_gateway_id", table_name="sandbox_runs")
    op.drop_index("ix_sandbox_runs_org_id", table_name="sandbox_runs")
    op.drop_table("sandbox_runs")
