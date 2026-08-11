"""add retry-safe payment ledger identity

Revision ID: d6a1b3c5e7f9
Revises: c5f9a2d4e6b8
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "d6a1b3c5e7f9"
down_revision = "c5f9a2d4e6b8"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        column["name"]
        for column in inspect(op.get_bind()).get_columns("payment_records")
    }


def _unique_constraints() -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(op.get_bind()).get_unique_constraints("payment_records")
        if constraint.get("name")
    }


def upgrade() -> None:
    columns = _columns()
    with op.batch_alter_table("payment_records") as batch:
        if "event_id" not in columns:
            batch.add_column(sa.Column("event_id", sa.String(length=64), nullable=True))
        if "reason" not in columns:
            batch.add_column(
                sa.Column("reason", sa.Text(), nullable=False, server_default="")
            )
        if "wallet_debited" not in columns:
            batch.add_column(
                sa.Column(
                    "wallet_debited",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )

    if "uq_payment_records_gateway_event" not in _unique_constraints():
        with op.batch_alter_table("payment_records") as batch:
            batch.create_unique_constraint(
                "uq_payment_records_gateway_event",
                ["gateway_id", "event_id"],
            )


def downgrade() -> None:
    if "uq_payment_records_gateway_event" in _unique_constraints():
        with op.batch_alter_table("payment_records") as batch:
            batch.drop_constraint(
                "uq_payment_records_gateway_event",
                type_="unique",
            )

    columns = _columns()
    with op.batch_alter_table("payment_records") as batch:
        for name in ("wallet_debited", "reason", "event_id"):
            if name in columns:
                batch.drop_column(name)
