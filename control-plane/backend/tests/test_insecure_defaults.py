"""Tests for #5: kill insecure defaults (JWT secret, admin/admin, open ingest).

Dev/demo stays permissive; production (OSTIARI_ENV=production) fails closed.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio

_FERNET_KEY = (  # gitleaks:allow - deterministic test-only Fernet key
    "-oUl3c_Lb7U-Z1JawknrorCyThuwnRMc_6leonQpjeo="
)


def _secure_production_env(monkeypatch):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
    monkeypatch.setenv("OSTIARI_NO_DEMO", "1")
    monkeypatch.setenv("OSTIARI_TENANCY_MODE", "single")
    monkeypatch.setenv("OSTIARI_ORG_ID", "production-org")
    monkeypatch.setenv("OSTIARI_CONTROL_PLANE_REPLICAS", "2")
    monkeypatch.setenv("REDIS_URL", "rediss://redis.internal:6379/0")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://ostiari:secret@db.internal:5432/ostiari",
    )
    monkeypatch.setenv("OSTIARI_JWT_SECRET", "j" * 40)
    monkeypatch.setenv("OSTIARI_ADMIN_PASSWORD", "a" * 20)
    monkeypatch.delenv("OSTIARI_INGEST_KEY", raising=False)
    monkeypatch.delenv("OSTIARI_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv(
        "OSTIARI_WORKLOAD_OIDC_ISSUER",
        "https://workload.example.com",
    )
    monkeypatch.setenv(
        "OSTIARI_WORKLOAD_OIDC_AUDIENCE",
        "ostiari-control-plane",
    )
    monkeypatch.setenv("OSTIARI_CONFIG_ADMIN_KEY", "c" * 40)
    monkeypatch.setenv("OSTIARI_GATEWAY_AGENT_TOKEN", "g" * 40)
    monkeypatch.setenv("OSTIARI_GATEWAY_AGENT_ID", "control-plane")
    monkeypatch.setenv("OSTIARI_ENCRYPTION_KEY", _FERNET_KEY)
    monkeypatch.setenv("OSTIARI_CORS_ORIGINS", "https://console.example.com")
    monkeypatch.setenv("OSTIARI_GATEWAY_CALLBACK_ALLOW", "gateway.internal")


class TestProductionPosture:
    def test_complete_configuration_passes(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        validate_production_posture()

    def test_auth_must_be_enabled(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.delenv("OSTIARI_REQUIRE_AUTH", raising=False)
        with pytest.raises(RuntimeError, match="OSTIARI_REQUIRE_AUTH"):
            validate_production_posture()

    def test_sqlite_is_rejected(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:////data/control.db")
        with pytest.raises(RuntimeError, match="postgresql\\+asyncpg"):
            validate_production_posture()

    def test_wildcard_cors_is_rejected(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.setenv("OSTIARI_CORS_ORIGINS", "*")
        with pytest.raises(RuntimeError, match="HTTPS origins"):
            validate_production_posture()

    def test_multi_tenant_mode_is_supported(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.setenv("OSTIARI_TENANCY_MODE", "multi")
        validate_production_posture()

    def test_unknown_tenancy_mode_is_rejected(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.setenv("OSTIARI_TENANCY_MODE", "shared")
        with pytest.raises(RuntimeError, match="OSTIARI_TENANCY_MODE"):
            validate_production_posture()

    def test_redis_is_required(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.delenv("REDIS_URL")
        with pytest.raises(RuntimeError, match="REDIS_URL"):
            validate_production_posture()

    def test_replica_count_must_be_positive(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.setenv("OSTIARI_CONTROL_PLANE_REPLICAS", "0")
        with pytest.raises(RuntimeError, match="OSTIARI_CONTROL_PLANE_REPLICAS"):
            validate_production_posture()

    def test_production_org_is_required(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.delenv("OSTIARI_ORG_ID")
        with pytest.raises(RuntimeError, match="OSTIARI_ORG_ID"):
            validate_production_posture()

    def test_gateway_callback_allowlist_is_required(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.delenv("OSTIARI_GATEWAY_CALLBACK_ALLOW")
        with pytest.raises(RuntimeError, match="OSTIARI_GATEWAY_CALLBACK_ALLOW"):
            validate_production_posture()

    def test_workload_oidc_is_required(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.delenv("OSTIARI_WORKLOAD_OIDC_ISSUER")
        with pytest.raises(RuntimeError, match="OSTIARI_WORKLOAD_OIDC_ISSUER"):
            validate_production_posture()

    def test_legacy_machine_credentials_are_rejected(self, monkeypatch):
        from control_plane.env import validate_production_posture

        _secure_production_env(monkeypatch)
        monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "legacy-secret")
        with pytest.raises(RuntimeError, match="legacy shared credential"):
            validate_production_posture()


# ── JWT secret ────────────────────────────────────────────────────────────

class TestJwtSecret:
    def test_dev_allows_default(self, monkeypatch):
        monkeypatch.delenv("OSTIARI_ENV", raising=False)
        monkeypatch.delenv("OSTIARI_JWT_SECRET", raising=False)
        from control_plane.auth.service import _resolve_jwt_secret
        from control_plane.env import DEFAULT_DEV_JWT_SECRET
        assert _resolve_jwt_secret() == DEFAULT_DEV_JWT_SECRET

    def test_prod_refuses_default(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.delenv("OSTIARI_JWT_SECRET", raising=False)
        from control_plane.auth.service import _resolve_jwt_secret
        with pytest.raises(RuntimeError, match="OSTIARI_JWT_SECRET"):
            _resolve_jwt_secret()

    def test_prod_refuses_short(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.setenv("OSTIARI_JWT_SECRET", "short")
        from control_plane.auth.service import _resolve_jwt_secret
        with pytest.raises(RuntimeError, match="too short"):
            _resolve_jwt_secret()

    def test_prod_accepts_strong(self, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.setenv("OSTIARI_JWT_SECRET", "x" * 40)
        from control_plane.auth.service import _resolve_jwt_secret
        assert len(_resolve_jwt_secret()) == 40

    def test_import_fails_fast_in_prod_with_default(self):
        # The module resolves JWT_SECRET at import; in prod without a secret it
        # must refuse to load (fail-fast), so the app can't start misconfigured.
        code = "import control_plane.auth.service"
        r = subprocess.run(
            [sys.executable, "-c", code],
            env={
                "OSTIARI_ENV": "production",
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            },
            capture_output=True, text=True, cwd=".",
        )
        assert r.returncode != 0
        assert "OSTIARI_JWT_SECRET" in (r.stderr + r.stdout)


# ── admin seed ──────────────────────────────────────────────────────────────

class TestAdminSeed:
    async def test_prod_without_admin_password_refuses(self, client, monkeypatch):
        # Seeding happens on first login; force a fresh seed attempt in prod.
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.delenv("OSTIARI_ADMIN_PASSWORD", raising=False)
        from control_plane.auth import router as auth_router
        with pytest.raises(RuntimeError, match="OSTIARI_ADMIN_PASSWORD"):
            from control_plane.database import get_db
            gen = get_db()
            db = await gen.__anext__()
            try:
                await auth_router._seed_admin(db)
            finally:
                await gen.aclose()

    async def test_dev_seeds_admin_admin(self, client, monkeypatch):
        monkeypatch.delenv("OSTIARI_ENV", raising=False)
        monkeypatch.delenv("OSTIARI_ADMIN_PASSWORD", raising=False)
        # login as the seeded admin works in dev
        r = await client.post("/api/auth/login",
                              json={
                                  "email": "admin@ostiari.ai",
                                  "password": "admin",
                                  "org_id": "default",
                              })
        assert r.status_code == 200
        assert "access_token" in r.json()

    async def test_demo_restores_documented_admin_password(self, client, monkeypatch):
        monkeypatch.delenv("OSTIARI_ENV", raising=False)
        monkeypatch.setenv("OSTIARI_NO_DEMO", "0")
        monkeypatch.setenv("OSTIARI_ADMIN_PASSWORD", "generated-evaluation-password")

        seeded = await client.post(
            "/api/auth/login",
            json={
                "email": "admin@ostiari.ai",
                "password": "generated-evaluation-password",
            },
        )
        assert seeded.status_code == 200

        monkeypatch.delenv("OSTIARI_ADMIN_PASSWORD")
        restored = await client.post(
            "/api/auth/login",
            json={"email": "admin@ostiari.ai", "password": "admin"},
        )
        assert restored.status_code == 200


# ── trace ingest ────────────────────────────────────────────────────────────

class TestIngest:
    async def test_dev_ingest_open(self, client, monkeypatch):
        monkeypatch.delenv("OSTIARI_ENV", raising=False)
        monkeypatch.delenv("OSTIARI_INGEST_KEY", raising=False)
        r = await client.post("/api/traces/ingest", json={"action": "x", "tier": "allow"})
        assert r.status_code == 200

    async def test_prod_ingest_requires_workload_identity(
        self,
        client,
        monkeypatch,
    ):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.delenv("OSTIARI_INGEST_KEY", raising=False)
        r = await client.post("/api/traces/ingest", json={"action": "x", "tier": "allow"})
        assert r.status_code == 401

    async def test_prod_ingest_rejects_legacy_key(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.setenv("OSTIARI_INGEST_KEY", "sekret")
        r = await client.post("/api/traces/ingest", json={"action": "x", "tier": "allow"},
                              headers={"X-Ingest-Key": "sekret"})
        assert r.status_code == 401
