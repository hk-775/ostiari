"""AWS entrypoint database and migration-lock contracts."""

from __future__ import annotations

import subprocess

import pytest
from control_plane import aws_entrypoint
from sqlalchemy.engine import make_url


def test_database_url_preserves_reserved_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OSTIARI_DB_HOST", "database.internal")
    monkeypatch.setenv("OSTIARI_DB_PORT", "5433")
    monkeypatch.setenv("OSTIARI_DB_NAME", "ostiari")
    monkeypatch.setenv("OSTIARI_DB_USER", "user@tenant")
    monkeypatch.setenv("OSTIARI_DB_PASSWORD", "p@ss:/word")

    settings = aws_entrypoint._database_settings()
    url = make_url(aws_entrypoint._database_url(settings))

    assert url.drivername == "postgresql+asyncpg"
    assert url.username == "user@tenant"
    assert url.password == "p@ss:/word"
    assert url.host == "database.internal"
    assert url.port == 5433
    assert url.query["ssl"] == "require"


@pytest.mark.asyncio
async def test_migrations_are_serialized_with_postgres_advisory_lock(monkeypatch) -> None:
    statements: list[tuple[str, int]] = []
    commands: list[tuple[list[str], str]] = []

    class Connection:
        async def execute(self, statement: str, lock_id: int) -> None:
            statements.append((statement, lock_id))

        async def close(self) -> None:
            statements.append(("close", 0))

    async def connect(**kwargs):
        assert kwargs["ssl"] == "require"
        return Connection()

    def run(command, *, cwd, env, check):
        assert check
        commands.append((command, env["DATABASE_URL"]))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(aws_entrypoint.asyncpg, "connect", connect)
    monkeypatch.setattr(aws_entrypoint.subprocess, "run", run)

    settings = {
        "host": "database.internal",
        "port": 5432,
        "database": "ostiari",
        "user": "ostiari",
        "password": "secret",
    }
    await aws_entrypoint._migrate(settings, "postgresql+asyncpg://example")

    assert statements == [
        ("SELECT pg_advisory_lock($1)", aws_entrypoint._MIGRATION_LOCK_ID),
        ("SELECT pg_advisory_unlock($1)", aws_entrypoint._MIGRATION_LOCK_ID),
        ("close", 0),
    ]
    assert commands == [
        (
            ["alembic", "-c", "/app/alembic.ini", "upgrade", "head"],
            "postgresql+asyncpg://example",
        )
    ]
