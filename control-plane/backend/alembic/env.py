"""Alembic migration environment (async engine, shared with the app).

Pulls the target metadata and DB URL from the application so migrations always
match what the app expects. `render_as_batch` is on so SQLite ALTER TABLE
(add column / index) works via batch table-rebuild.
"""

import asyncio
import os
from logging.config import fileConfig

# Register the auth tables (users/sessions) on Base.metadata, mirroring app.py.
import control_plane.auth.models  # noqa: F401
from alembic import context

# default_sqlite_url is the app's own default, shared rather than duplicated: this
# file and control_plane.database both call it, so `alembic upgrade head` with no
# DATABASE_URL cannot migrate a different file than the app opens. Importing
# control_plane.env is safe — unlike control_plane.database, it creates no engine
# (which would bind to a migration run's scratch DATABASE_URL and leak into the
# engine the app reuses) and performs no import-time filesystem write.
from control_plane.env import default_sqlite_url
from control_plane.models.database import Base
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Resolve the URL FRESH from the environment on every migration run.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or default_sqlite_url()

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ConfigParser treats percent signs in URL-encoded credentials as interpolation
# markers. Escape only the value stored in Alembic's config; engine construction
# below continues to use the original URL.
config.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
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
