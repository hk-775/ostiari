"""Alembic migration environment (async engine, shared with the app).

Pulls the target metadata and DB URL from the application so migrations always
match what the app expects. `render_as_batch` is on so SQLite ALTER TABLE
(add column / index) works via batch table-rebuild.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from control_plane.models.database import Base

# Register the auth tables (users/sessions) on Base.metadata, mirroring app.py.
import control_plane.auth.models  # noqa: F401,E402

# Resolve the URL FRESH from the environment. We intentionally do NOT import
# control_plane.database here: doing so would bind that module's global `engine`
# to whatever DATABASE_URL is set during a migration run (e.g. a test's scratch
# DB), leaking into the app engine other code reuses. Mirror its default path
# instead.
from pathlib import Path

# control_plane/database.py resolves this as <repo>/control-plane/data — three
# levels up from control_plane/database.py. This file sits one level deeper
# (alembic/env.py), so it needs three .parent hops to land on the same dir.
_DB_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_DEFAULT_URL = f"sqlite+aiosqlite:///{_DB_DIR / 'control_plane.db'}"
DATABASE_URL = os.environ.get("DATABASE_URL", _DEFAULT_URL)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", DATABASE_URL)
target_metadata = Base.metadata


def _run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite-safe ALTER via batch mode
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL, target_metadata=target_metadata,
        literal_binds=True, render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": DATABASE_URL},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
