"""Database connection and session management.

Supports PostgreSQL (production) and SQLite (development).

Set DATABASE_URL environment variable:
  PostgreSQL: postgresql+asyncpg://user:pass@host:5432/ostiari
  SQLite:     sqlite+aiosqlite:///path/to/db.sqlite (default for dev)
"""

import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from control_plane.env import default_sqlite_url

# Resolved lazily: default_sqlite_url() is only called when DATABASE_URL is unset,
# so importing this module in a container (where DATABASE_URL is always set) does
# not touch the filesystem. It used to mkdir unconditionally at import time, which
# a read-only root filesystem refuses — and at *import* time, so it raised before
# any application code ran and the container never answered a probe.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip() or default_sqlite_url()

_is_postgres = DATABASE_URL.startswith("postgresql")

_engine_kwargs = {
    "echo": os.environ.get("DB_ECHO", "").lower() == "true",
}

if _is_postgres:
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_size"] = 20
    _engine_kwargs["max_overflow"] = 10
else:
    _engine_kwargs["poolclass"] = NullPool

engine = create_async_engine(DATABASE_URL, **_engine_kwargs)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
