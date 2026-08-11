"""Merge payment-ledger and Sandbox migration heads.

Revision ID: f9a1b3c5d7e9
Revises: d6a1b3c5e7f9, e8f0a2b4c6d8
Create Date: 2026-08-11
"""

from collections.abc import Sequence

revision = "f9a1b3c5d7e9"
down_revision: tuple[str, str] = (
    "d6a1b3c5e7f9",
    "e8f0a2b4c6d8",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
