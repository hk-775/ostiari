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

import base64
import os
import tempfile
import time

# Unique temp DB file, set before any control_plane import (module-level engine).
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"

# Fixed Fernet key so provider api-key encrypt/decrypt is stable across calls
# (otherwise a fresh transient key is generated per call and decryption fails).
os.environ.setdefault(  # gitleaks:allow - deterministic test-only Fernet key
    "OSTIARI_ENCRYPTION_KEY",
    "-oUl3c_Lb7U-Z1JawknrorCyThuwnRMc_6leonQpjeo=",
)

import atexit  # noqa: E402

import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

atexit.register(lambda: os.path.exists(_DB_PATH) and os.remove(_DB_PATH))

WORKLOAD_ISSUER = "https://workload.test/issuer"
WORKLOAD_AUDIENCE = "ostiari-control-plane"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _reset_in_memory_state() -> None:
    """Clear module-level state used by in-memory routers between tests."""
    from control_plane import persistence
    from control_plane.auth import router as auth_router
    from control_plane.auth import sso, sso_router
    from control_plane.routers import (
        agent_routing,
        agents,
        approvals,
        experiments,
        model_config,
        payments,
        providers,
        quotas,
        roi,
        token_broker,
        traces,
        trust,
    )

    for mod, attr in (
        (sso_router, "_pending_states"),
        (agents, "_agents"),
        (approvals, "_pending"),
        (agent_routing, "_policies"),
        (experiments, "_experiments"),
        (model_config, "_models"),
        (providers, "_providers"),
        (quotas, "_quotas"),
        # _next_id too: leaving it set would make a later test's first quota id
        # continue from a previous test's count.
        (quotas, "_next_id"),
        (quotas, "_alerts"),
        (payments, "_pricing"),
        (roi, "_cost_model"),
        (token_broker, "_config"),
        (traces, "_recent_traces"),
        (traces, "_session_parents"),
        (traces, "_ws_clients"),
        (trust, "_enforced"),
    ):
        obj = getattr(mod, attr, None)
        if obj is not None and hasattr(obj, "clear"):
            obj.clear()
    auth_router._seeded = False
    persistence._loaded_runtime_revisions.clear()
    persistence._runtime_sync_error = ""
    for operation in traces._trace_bus_errors:
        traces._trace_bus_errors[operation] = ""
    sso.clear_caches()


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


def _base64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def workload_signer(monkeypatch):
    """Issue signed gateway workload tokens through a real JWKS validator."""
    from control_plane.auth import oidc, workload

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "workload-key",
                "use": "sig",
                "alg": "RS256",
                "n": _base64url_uint(public.n),
                "e": _base64url_uint(public.e),
            }
        ]
    }
    private_key = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    validator = oidc.OIDCValidator(
        issuer=WORKLOAD_ISSUER,
        jwks_url=f"{WORKLOAD_ISSUER}/jwks",
        audience=WORKLOAD_AUDIENCE,
        http_get=lambda _: jwks,
    )
    monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
    monkeypatch.setenv("OSTIARI_WORKLOAD_OIDC_ISSUER", WORKLOAD_ISSUER)
    monkeypatch.setenv("OSTIARI_WORKLOAD_OIDC_AUDIENCE", WORKLOAD_AUDIENCE)
    monkeypatch.setattr(workload, "get_workload_validator", lambda: validator)

    def issue(
        gateway_id: str,
        *,
        subject: str | None = None,
        tenant_id: str | None = "default",
        include_gateway_id: bool = True,
    ) -> dict[str, str]:
        now = int(time.time())
        claims = {
            "sub": subject or f"subject:{gateway_id}",
            "iss": WORKLOAD_ISSUER,
            "aud": WORKLOAD_AUDIENCE,
            "iat": now,
            "exp": now + 300,
            "client_id": f"client:{gateway_id}",
        }
        if tenant_id is not None:
            claims["tenant_id"] = tenant_id
        if include_gateway_id:
            claims["gateway_id"] = gateway_id
        token = jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": "workload-key"},
        )
        return {"Authorization": f"Bearer {token}"}

    yield issue
    workload.reset_workload_validator()
