"""scope the broker pilot tables to an org

`token_pools` and `reconciliation_records` were the last tenant tables without an
`org_id`. Adds one to both, backfilled to 'default', and — for `token_pools` —
folds it into the primary key.

The composite key is the part that needs a table rebuild rather than a plain
`ADD COLUMN`: the old PK was `provider` alone, so two orgs could not each hold an
"anthropic" pool. `batch_alter_table` does that rebuild portably (SQLite has no
`ALTER TABLE ... DROP CONSTRAINT`, so it copies into a new table and swaps).

`reconciliation_records` keeps its surrogate `id` PK and only gains an indexed
column, which is the same shape as every other tenant table.

Both steps are existence-guarded, so this is a no-op on a database created after
the model change (fresh installs get the final schema from `Base.metadata`).

Revision ID: 9a2d5e7c1b04
Revises: 8f1b2c3d4e5f
Create Date: 2026-08-05
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "9a2d5e7c1b04"
down_revision = "8f1b2c3d4e5f"
branch_labels = None
depends_on = None


def _insp():
    return inspect(op.get_bind())


def _pools_table(pk_cols: tuple[str, ...], with_org: bool) -> sa.Table:
    """The `token_pools` shape to rebuild against, stated in full.

    `batch_alter_table` needs `copy_from` to change a primary key: without it,
    batch mode reflects the *existing* table and reconstructs the old PK on the
    copy, so the new key is silently dropped. (Verified — the first version of
    this migration passed `table_args` instead and the rebuilt DDL still read
    `PRIMARY KEY (provider)`, with two SAWarnings that cancelled each other out.)
    """
    cols = [
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("purchased_tokens", sa.Integer(), nullable=False),
        sa.Column("purchased_cost_usd", sa.Float(), nullable=False),
        sa.Column("consumed_tokens", sa.Integer(), nullable=False),
        sa.Column("consumed_cost_usd", sa.Float(), nullable=False),
        sa.Column("low_threshold_tokens", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    ]
    if with_org:
        cols.append(sa.Column("org_id", sa.String(length=64), nullable=False))
    return sa.Table(
        "token_pools", sa.MetaData(), *cols, sa.PrimaryKeyConstraint(*pk_cols),
    )


def upgrade() -> None:
    insp = _insp()
    tables = set(insp.get_table_names())

    # 1. reconciliation_records — indexed column, backfilled. Same as the other
    #    tenant tables; the surrogate id stays the primary key.
    if "reconciliation_records" in tables:
        cols = {c["name"] for c in insp.get_columns("reconciliation_records")}
        if "org_id" not in cols:
            with op.batch_alter_table("reconciliation_records", schema=None) as batch:
                batch.add_column(sa.Column("org_id", sa.String(length=64), nullable=True))
                batch.create_index("ix_reconciliation_records_org_id", ["org_id"], unique=False)
            op.execute(sa.text(
                "UPDATE reconciliation_records SET org_id='default' WHERE org_id IS NULL"
            ))

    # 2. token_pools — org_id joins the primary key, so the table is rebuilt.
    #
    #    Order matters: the column is added nullable and backfilled *before* the
    #    PK is redefined, because a NULL can't be part of a primary key. Doing it
    #    in one batch block would try to build the new key over rows that don't
    #    have the value yet.
    if "token_pools" in tables:
        cols = {c["name"] for c in insp.get_columns("token_pools")}
        if "org_id" not in cols:
            with op.batch_alter_table("token_pools", schema=None) as batch:
                batch.add_column(sa.Column("org_id", sa.String(length=64), nullable=True))
            op.execute(sa.text("UPDATE token_pools SET org_id='default' WHERE org_id IS NULL"))
            with op.batch_alter_table(
                "token_pools",
                schema=None,
                copy_from=_pools_table(("provider", "org_id"), with_org=True),
                # `recreate="always"` is load-bearing. Batch mode rebuilds only
                # when an operation demands it, and `create_index` doesn't — so
                # with the default "auto" the copy_from shape is never applied
                # and the table keeps `PRIMARY KEY (provider)`. Verified: that
                # was the second wrong version of this migration.
                recreate="always",
            ) as batch:
                batch.create_index("ix_token_pools_org_id", ["org_id"], unique=False)


def downgrade() -> None:
    insp = _insp()
    tables = set(insp.get_table_names())

    # token_pools: back to provider as the sole primary key. This is lossy by
    # nature — if more than one org funded the same provider, collapsing the key
    # would collide, so drop the non-default rows first and say so. A pilot
    # rolling back has not yet onboarded a second tenant; a deployment that has
    # should not be running this.
    if "token_pools" in tables:
        cols = {c["name"] for c in insp.get_columns("token_pools")}
        if "org_id" in cols:
            op.execute(sa.text("DELETE FROM token_pools WHERE org_id <> 'default'"))
            # Outside the batch block, not inside it: `copy_from` describes the
            # table's columns and key, not its indexes, so a batch `drop_index`
            # raises "No such index" against that description.
            op.drop_index("ix_token_pools_org_id", table_name="token_pools")
            with op.batch_alter_table(
                "token_pools",
                schema=None,
                copy_from=_pools_table(("provider", "org_id"), with_org=True),
                recreate="always",
            ) as batch:
                batch.drop_column("org_id")

    if "reconciliation_records" in tables:
        cols = {c["name"] for c in insp.get_columns("reconciliation_records")}
        if "org_id" in cols:
            op.drop_index("ix_reconciliation_records_org_id", table_name="reconciliation_records")
            with op.batch_alter_table("reconciliation_records", schema=None) as batch:
                batch.drop_column("org_id")
