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

engine = create_async_engine(
    DATABASE_URL,
    echo=os.environ.get("DB_ECHO", "").lower() == "true",
    pool_pre_ping=True if _is_postgres else False,
    poolclass=NullPool if not _is_postgres else None,
    pool_size=20 if _is_postgres else None,
    max_overflow=10 if _is_postgres else None,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
