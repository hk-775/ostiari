"""add idempotent broker accounting to usage records

Revision ID: b4e8f0a1c2d3
Revises: 9a2d5e7c1b04
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "b4e8f0a1c2d3"
down_revision = "9a2d5e7c1b04"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns("usage_records")}


def _unique_constraints() -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(op.get_bind()).get_unique_constraints("usage_records")
        if constraint.get("name")
    }


def upgrade() -> None:
    columns = _columns()
    with op.batch_alter_table("usage_records") as batch:
        if "event_id" not in columns:
            batch.add_column(sa.Column("event_id", sa.String(length=64), nullable=True))
        if "provider" not in columns:
            batch.add_column(
                sa.Column("provider", sa.String(length=64), nullable=False, server_default="")
            )
        if "broker_cost_usd" not in columns:
            batch.add_column(
                sa.Column("broker_cost_usd", sa.Float(), nullable=False, server_default="0")
            )
        if "broker_charge_usd" not in columns:
            batch.add_column(
                sa.Column("broker_charge_usd", sa.Float(), nullable=False, server_default="0")
            )
        if "billing_status" not in columns:
            batch.add_column(
                sa.Column(
                    "billing_status",
                    sa.String(length=20),
                    nullable=False,
                    server_default="not_applicable",
                )
            )
        if "billing_ref" not in columns:
            batch.add_column(
                sa.Column("billing_ref", sa.String(length=128), nullable=False, server_default="")
            )
        if "billing_error" not in columns:
            batch.add_column(sa.Column("billing_error", sa.Text(), nullable=False, server_default=""))

    if "uq_usage_records_gateway_event" not in _unique_constraints():
        with op.batch_alter_table("usage_records") as batch:
            batch.create_unique_constraint(
                "uq_usage_records_gateway_event",
                ["gateway_id", "event_id"],
            )


def downgrade() -> None:
    if "uq_usage_records_gateway_event" in _unique_constraints():
        with op.batch_alter_table("usage_records") as batch:
            batch.drop_constraint("uq_usage_records_gateway_event", type_="unique")

    columns = _columns()
    with op.batch_alter_table("usage_records") as batch:
        for name in (
            "billing_error",
            "billing_ref",
            "billing_status",
            "broker_charge_usd",
            "broker_cost_usd",
            "provider",
            "event_id",
        ):
            if name in columns:
                batch.drop_column(name)
