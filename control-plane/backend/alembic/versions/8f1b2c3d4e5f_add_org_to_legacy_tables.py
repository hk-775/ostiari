"""add org_id to legacy (pre-multi-tenancy) tables

For deployments whose DB was created BEFORE multi-tenancy (i.e. tables exist
without org_id and without the organizations table). Fresh installs get the
full schema from the baseline revision and this migration is then a no-op
(every column/table already present — each step is existence-guarded).

Revision ID: 8f1b2c3d4e5f
Revises: 2c8a75232ada
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "8f1b2c3d4e5f"
down_revision = "2c8a75232ada"
branch_labels = None
depends_on = None

# Tenant tables that gain an org_id (foundational slice + the follow-up tables
# mcp_servers / a2a_agents / payment_records). Each add is existence-guarded so
# fresh DBs (baseline already has the column) are a no-op.
_ORG_TABLES = ("users", "gateways", "tools", "policies", "wallets",
               "usage_records", "audit_logs",
               "mcp_servers", "a2a_agents", "payment_records")


def _insp():
    return inspect(op.get_bind())


def upgrade() -> None:
    insp = _insp()
    tables = set(insp.get_table_names())

    # 1. organizations table + default row (skip if the baseline already made it).
    if "organizations" not in tables:
        op.create_table(
            "organizations",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    # Insert default org only if absent (portable existence check).
    bind = op.get_bind()
    exists = bind.execute(sa.text("SELECT 1 FROM organizations WHERE id='default'")).first()
    if not exists:
        op.execute(sa.text(
            "INSERT INTO organizations (id, name, created_at) "
            "VALUES ('default', 'Default Organization', CURRENT_TIMESTAMP)"
        ))

    # 2. org_id column + index on each legacy table that lacks it, backfilled.
    for table in _ORG_TABLES:
        if table not in tables:
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if "org_id" in cols:
            continue
        with op.batch_alter_table(table, schema=None) as batch:
            batch.add_column(sa.Column("org_id", sa.String(length=64), nullable=True))
            batch.create_index(f"ix_{table}_org_id", ["org_id"], unique=False)
        op.execute(sa.text(f"UPDATE {table} SET org_id='default' WHERE org_id IS NULL"))


def downgrade() -> None:
    # No-op: on a fresh install the baseline revision owns the org columns,
    # organizations table, and indexes (this migration's upgrade was a no-op),
    # so tearing them down here would double-drop what the baseline downgrade
    # removes. For a genuine pre-org DB there is no earlier revision to return
    # to, so a downgrade past the baseline isn't a supported path.
    pass
