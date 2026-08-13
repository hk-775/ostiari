"""Add durable approvals, traces, SSO state, and serialized audit head.

Revision ID: b3d5f7a9c1e3
Revises: a2c4e6f8b0d2
Create Date: 2026-08-13
"""

from collections.abc import Sequence
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "b3d5f7a9c1e3"
down_revision = "a2c4e6f8b0d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_chain_heads",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("entry_hash", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )

    bind = op.get_bind()
    latest_hash = bind.execute(
        sa.text(
            "SELECT entry_hash FROM audit_logs "
            "WHERE entry_hash IS NOT NULL ORDER BY id DESC LIMIT 1"
        )
    ).scalar()
    bind.execute(
        sa.text(
            "INSERT INTO audit_chain_heads (name, entry_hash, updated_at) "
            "VALUES (:name, :entry_hash, :updated_at)"
        ),
        {
            "name": "global",
            "entry_hash": latest_hash or "",
            "updated_at": datetime.now(timezone.utc),
        },
    )

    op.create_table(
        "approval_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=128), nullable=False),
        sa.Column("gateway_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("params_encrypted", sa.Text(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("decided_by", sa.String(length=256), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'expired')",
            name="ck_approval_records_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_approval_records_gateway_id",
        "approval_records",
        ["gateway_id"],
        unique=False,
    )
    op.create_index(
        "ix_approval_records_org_id",
        "approval_records",
        ["org_id"],
        unique=False,
    )
    op.create_index(
        "ix_approval_records_status",
        "approval_records",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_approval_records_org_status_created",
        "approval_records",
        ["org_id", "status", "created_at"],
        unique=False,
    )

    op.create_table(
        "trace_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("gateway_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "trace_id",
            name="uq_trace_records_org_trace_id",
        ),
    )
    op.create_index(
        "ix_trace_records_gateway_id",
        "trace_records",
        ["gateway_id"],
        unique=False,
    )
    op.create_index(
        "ix_trace_records_org_id",
        "trace_records",
        ["org_id"],
        unique=False,
    )
    op.create_index(
        "ix_trace_records_org_updated",
        "trace_records",
        ["org_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "sso_login_states",
        sa.Column("state_digest", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.String(length=256), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state_digest"),
    )
    op.create_index(
        "ix_sso_login_states_expires_at",
        "sso_login_states",
        ["expires_at"],
        unique=False,
    )

    op.create_table(
        "runtime_state_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("item_key", sa.String(length=256), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "namespace",
            "item_key",
            name="uq_runtime_state_org_namespace_key",
        ),
    )
    op.create_index(
        "ix_runtime_state_records_org_id",
        "runtime_state_records",
        ["org_id"],
        unique=False,
    )
    op.create_index(
        "ix_runtime_state_org_namespace",
        "runtime_state_records",
        ["org_id", "namespace"],
        unique=False,
    )
    op.create_table(
        "runtime_state_sequences",
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.Column("namespace", sa.String(length=64), nullable=False),
        sa.Column("next_value", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("org_id", "namespace"),
    )
    op.create_table(
        "login_attempt_windows",
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key_digest"),
    )


def downgrade() -> None:
    op.drop_table("login_attempt_windows")
    op.drop_table("runtime_state_sequences")
    op.drop_index(
        "ix_runtime_state_org_namespace",
        table_name="runtime_state_records",
    )
    op.drop_index(
        "ix_runtime_state_records_org_id",
        table_name="runtime_state_records",
    )
    op.drop_table("runtime_state_records")

    op.drop_index("ix_sso_login_states_expires_at", table_name="sso_login_states")
    op.drop_table("sso_login_states")

    op.drop_index("ix_trace_records_org_updated", table_name="trace_records")
    op.drop_index("ix_trace_records_org_id", table_name="trace_records")
    op.drop_index("ix_trace_records_gateway_id", table_name="trace_records")
    op.drop_table("trace_records")

    op.drop_index(
        "ix_approval_records_org_status_created",
        table_name="approval_records",
    )
    op.drop_index("ix_approval_records_status", table_name="approval_records")
    op.drop_index("ix_approval_records_org_id", table_name="approval_records")
    op.drop_index("ix_approval_records_gateway_id", table_name="approval_records")
    op.drop_table("approval_records")

    op.drop_table("audit_chain_heads")
