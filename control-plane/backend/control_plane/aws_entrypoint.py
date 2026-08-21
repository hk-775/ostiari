"""AWS container entrypoint with serialized PostgreSQL migrations."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import asyncpg
import uvicorn
from sqlalchemy.engine import URL

_MIGRATION_LOCK_ID = 6_734_198_421


def _database_settings() -> dict[str, str | int]:
    required = (
        "OSTIARI_DB_HOST",
        "OSTIARI_DB_NAME",
        "OSTIARI_DB_USER",
        "OSTIARI_DB_PASSWORD",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("Missing AWS database settings: " + ", ".join(sorted(missing)))
    return {
        "host": os.environ["OSTIARI_DB_HOST"],
        "port": int(os.environ.get("OSTIARI_DB_PORT", "5432")),
        "database": os.environ["OSTIARI_DB_NAME"],
        "user": os.environ["OSTIARI_DB_USER"],
        "password": os.environ["OSTIARI_DB_PASSWORD"],
    }


def _database_url(settings: dict[str, str | int]) -> str:
    return URL.create(
        "postgresql+asyncpg",
        username=str(settings["user"]),
        password=str(settings["password"]),
        host=str(settings["host"]),
        port=int(settings["port"]),
        database=str(settings["database"]),
        query={"ssl": "require"},
    ).render_as_string(hide_password=False)


async def _migrate(settings: dict[str, str | int], database_url: str) -> None:
    connection = await asyncpg.connect(**settings, ssl="require")
    try:
        await connection.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID)
        env = os.environ.copy()
        env["DATABASE_URL"] = database_url
        subprocess.run(
            [
                "alembic",
                "-c",
                str(Path("/app/alembic.ini")),
                "upgrade",
                "head",
            ],
            cwd="/app",
            env=env,
            check=True,
        )
    finally:
        await connection.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID)
        await connection.close()


def main() -> None:
    settings = _database_settings()
    database_url = _database_url(settings)
    os.environ["DATABASE_URL"] = database_url
    asyncio.run(_migrate(settings, database_url))

    from control_plane.app import app

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8400,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
