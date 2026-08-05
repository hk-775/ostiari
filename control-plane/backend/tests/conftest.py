"""Shared fixtures for control-plane backend tests.

Provides an isolated SQLite database and an async TestClient per test, plus
auth helpers (real JWTs) for admin and viewer roles.

A unique temp-file SQLite DB is used (not :memory:) because the app's engine
uses NullPool — with NullPool each connection to :memory: gets a *fresh* empty
database, so schema created on one connection is invisible to the next. A file
is shared across connections. DATABASE_URL is set before importing
control_plane.database so the module-level engine binds to it.
"""

from __future__ import annotations

import os
import tempfile

# Unique temp DB file, set before any control_plane import (module-level engine).
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"

# Fixed Fernet key so provider api-key encrypt/decrypt is stable across calls
# (otherwise a fresh transient key is generated per call and decryption fails).
os.environ.setdefault("OSTIARI_ENCRYPTION_KEY", "-oUl3c_Lb7U-Z1JawknrorCyThuwnRMc_6leonQpjeo=")

import atexit  # noqa: E402

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

atexit.register(lambda: os.path.exists(_DB_PATH) and os.remove(_DB_PATH))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _reset_in_memory_state() -> None:
    """Clear module-level state used by in-memory routers between tests."""
    from control_plane.routers import (
        agent_routing,
        agents,
        approvals,
        model_config,
        providers,
        quotas,
        traces,
    )

    for mod, attr in (
        (agents, "_agents"),
        (approvals, "_pending"),
        (agent_routing, "_policies"),
        (model_config, "_models"),
        (providers, "_providers"),
        (quotas, "_quotas"),
        # _next_id too: leaving it set would make a later test's first quota id
        # continue from a previous test's count.
        (quotas, "_next_id"),
        (traces, "_recent_traces"),
        (traces, "_session_parents"),
        (traces, "_ws_clients"),
    ):
        obj = getattr(mod, attr, None)
        if obj is not None and hasattr(obj, "clear"):
            obj.clear()


@pytest.fixture
async def app_and_db():
    """Fresh schema per test on the shared DB file; drop tables after."""
    from control_plane.app import app
    from control_plane.database import engine
    from control_plane.models.database import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    _reset_in_memory_state()
    yield app


@pytest.fixture
async def client(app_and_db) -> AsyncClient:
    """Async HTTP client bound to the app (in-process, no network)."""
    transport = ASGITransport(app=app_and_db)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ─── Auth helpers ───────────────────────────────────────────────────────────

def _token(user_id: int, email: str, role: str) -> str:
    from control_plane.auth.service import create_access_token

    return create_access_token(user_id=user_id, email=email, role=role)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(1, 'admin@test.io', 'admin')}"}


@pytest.fixture
def viewer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(2, 'viewer@test.io', 'viewer')}"}
