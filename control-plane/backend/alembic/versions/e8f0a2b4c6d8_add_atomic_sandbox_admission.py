"""Add database-enforced Sandbox run admission slots.

Revision ID: e8f0a2b4c6d8
Revises: d7e9f1a3b5c7
Create Date: 2026-08-11
"""

from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "e8f0a2b4c6d8"
down_revision = "d7e9f1a3b5c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK_NAME = "ck_sandbox_runs_running_has_slot"
_UNIQUE_NAME = "uq_sandbox_runs_org_active_slot"


def upgrade() -> None:
    op.add_column(
        "sandbox_runs",
        sa.Column("active_slot", sa.Integer(), nullable=True),
    )

    runs = sa.table(
        "sandbox_runs",
        sa.column("id", sa.String(length=36)),
        sa.column("org_id", sa.String(length=64)),
        sa.column("status", sa.String(length=20)),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("active_slot", sa.Integer()),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(runs.c.id, runs.c.org_id)
        .where(runs.c.status == "running")
        .order_by(runs.c.org_id, runs.c.started_at, runs.c.id)
    ).all()
    next_slot: defaultdict[str, int] = defaultdict(int)
    for run_id, org_id in rows:
        bind.execute(
            runs.update()
            .where(runs.c.id == run_id)
            .values(active_slot=next_slot[org_id])
        )
        next_slot[org_id] += 1

    with op.batch_alter_table("sandbox_runs") as batch:
        batch.create_unique_constraint(
            _UNIQUE_NAME,
            ["org_id", "active_slot"],
        )
        batch.create_check_constraint(
            _CHECK_NAME,
            "status != 'running' OR active_slot IS NOT NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("sandbox_runs") as batch:
        batch.drop_constraint(_CHECK_NAME, type_="check")
        batch.drop_constraint(_UNIQUE_NAME, type_="unique")
        batch.drop_column("active_slot")
