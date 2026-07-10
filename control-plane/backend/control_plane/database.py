"""Database connection and session management.

Supports PostgreSQL (production) and SQLite (development).

Set DATABASE_URL environment variable:
  PostgreSQL: postgresql+asyncpg://user:pass@host:5432/ostiari
  SQLite:     sqlite+aiosqlite:///path/to/db.sqlite (default for dev)
"""

import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

_DB_DIR = Path(__file__).parent.parent.parent / "data"
_DB_DIR.mkdir(exist_ok=True)
_DEFAULT_DB = f"sqlite+aiosqlite:///{_DB_DIR / 'control_plane.db'}"

DATABASE_URL = os.environ.get("DATABASE_URL", _DEFAULT_DB)

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
