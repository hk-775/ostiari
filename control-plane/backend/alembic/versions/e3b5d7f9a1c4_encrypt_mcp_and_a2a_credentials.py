"""Encrypt persisted MCP configuration and A2A authentication tokens.

Revision ID: e3b5d7f9a1c4
Revises: d8f1a3c5e7b9
Create Date: 2026-08-26
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "e3b5d7f9a1c4"
down_revision = "d8f1a3c5e7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _cipher():
    key = os.environ.get("OSTIARI_ENCRYPTION_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OSTIARI_ENCRYPTION_KEY is required to migrate stored MCP or "
            "A2A credentials"
        )
    from cryptography.fernet import Fernet

    try:
        return Fernet(key.encode())
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "OSTIARI_ENCRYPTION_KEY must be a valid Fernet key"
        ) from exc


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("Legacy MCP config must be a JSON object")


def _encrypt(plaintext: str, cipher: Any) -> str:
    return cipher.encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str, cipher: Any) -> str:
    return cipher.decrypt(ciphertext.encode()).decode()


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column(
            "config_encrypted",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "a2a_agents",
        sa.Column(
            "auth_token_encrypted",
            sa.Text(),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )

    bind = op.get_bind()
    mcp_servers = sa.table(
        "mcp_servers",
        sa.column("id", sa.Integer()),
        sa.column("config", sa.JSON()),
        sa.column("config_encrypted", sa.Text()),
    )
    a2a_agents = sa.table(
        "a2a_agents",
        sa.column("id", sa.Integer()),
        sa.column("auth_token", sa.String(length=512)),
        sa.column("auth_token_encrypted", sa.Text()),
    )

    mcp_rows = list(
        bind.execute(
            sa.select(mcp_servers.c.id, mcp_servers.c.config)
        ).mappings()
    )
    a2a_rows = list(
        bind.execute(
            sa.select(a2a_agents.c.id, a2a_agents.c.auth_token)
        ).mappings()
    )
    has_private_data = any(
        _json_object(row["config"]) for row in mcp_rows
    ) or any(str(row["auth_token"] or "") for row in a2a_rows)
    cipher = _cipher() if has_private_data else None

    for row in mcp_rows:
        document = _json_object(row["config"])
        if not document:
            continue
        plaintext = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
        )
        bind.execute(
            mcp_servers.update()
            .where(mcp_servers.c.id == row["id"])
            .values(
                config={},
                config_encrypted=_encrypt(plaintext, cipher),
            )
        )

    for row in a2a_rows:
        token = str(row["auth_token"] or "")
        if not token:
            continue
        bind.execute(
            a2a_agents.update()
            .where(a2a_agents.c.id == row["id"])
            .values(
                auth_token="",
                auth_token_encrypted=_encrypt(token, cipher),
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    mcp_servers = sa.table(
        "mcp_servers",
        sa.column("id", sa.Integer()),
        sa.column("config", sa.JSON()),
        sa.column("config_encrypted", sa.Text()),
    )
    a2a_agents = sa.table(
        "a2a_agents",
        sa.column("id", sa.Integer()),
        sa.column("auth_token", sa.String(length=512)),
        sa.column("auth_token_encrypted", sa.Text()),
    )

    mcp_rows = list(
        bind.execute(
            sa.select(
                mcp_servers.c.id,
                mcp_servers.c.config_encrypted,
            ).where(mcp_servers.c.config_encrypted != "")
        ).mappings()
    )
    a2a_rows = list(
        bind.execute(
            sa.select(
                a2a_agents.c.id,
                a2a_agents.c.auth_token_encrypted,
            ).where(a2a_agents.c.auth_token_encrypted != "")
        ).mappings()
    )
    cipher = _cipher() if mcp_rows or a2a_rows else None

    for row in mcp_rows:
        document = _json_object(
            _decrypt(row["config_encrypted"], cipher)
        )
        bind.execute(
            mcp_servers.update()
            .where(mcp_servers.c.id == row["id"])
            .values(config=document)
        )

    for row in a2a_rows:
        bind.execute(
            a2a_agents.update()
            .where(a2a_agents.c.id == row["id"])
            .values(
                auth_token=_decrypt(
                    row["auth_token_encrypted"],
                    cipher,
                )
            )
        )

    with op.batch_alter_table("a2a_agents") as batch:
        batch.drop_column("auth_token_encrypted")
    with op.batch_alter_table("mcp_servers") as batch:
        batch.drop_column("config_encrypted")
