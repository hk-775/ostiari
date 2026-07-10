"""Database connection and session management."""

import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Store DB in a fixed location so data persists across restarts
_DB_DIR = Path(__file__).parent.parent.parent / "data"
_DB_DIR.mkdir(exist_ok=True)
_DEFAULT_DB = f"sqlite+aiosqlite:///{_DB_DIR / 'control_plane.db'}"

DATABASE_URL = os.environ.get("DATABASE_URL", _DEFAULT_DB)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session
