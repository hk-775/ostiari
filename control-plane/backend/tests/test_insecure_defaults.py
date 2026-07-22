"""Tests for #5: kill insecure defaults (JWT secret, admin/admin, open ingest).

Dev/demo stays permissive; production (OSTIARI_ENV=production) fails closed.
"""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.anyio


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
            env={"OSTIARI_ENV": "production", "PATH": "/usr/bin:/bin"},
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
        monkeypatch.setattr(auth_router, "_seeded", False)
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
        # login as the seeded admin works in dev
        r = await client.post("/api/auth/login",
                              json={"email": "admin@ostiari.ai", "password": "admin"})
        assert r.status_code == 200
        assert "access_token" in r.json()


# ── trace ingest ────────────────────────────────────────────────────────────

class TestIngest:
    async def test_dev_ingest_open(self, client, monkeypatch):
        monkeypatch.delenv("OSTIARI_ENV", raising=False)
        monkeypatch.delenv("OSTIARI_INGEST_KEY", raising=False)
        r = await client.post("/api/traces/ingest", json={"action": "x", "tier": "allow"})
        assert r.status_code == 200

    async def test_prod_ingest_requires_key(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.delenv("OSTIARI_INGEST_KEY", raising=False)
        r = await client.post("/api/traces/ingest", json={"action": "x", "tier": "allow"})
        assert r.status_code == 401

    async def test_prod_ingest_with_key_ok(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_ENV", "production")
        monkeypatch.setenv("OSTIARI_INGEST_KEY", "sekret")
        r = await client.post("/api/traces/ingest", json={"action": "x", "tier": "allow"},
                              headers={"X-Ingest-Key": "sekret"})
        assert r.status_code == 200
