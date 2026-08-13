"""Tests for authentication, JWT service, RBAC, SSO, and the auth router."""

import pytest
from control_plane.auth import oidc, rbac, service, sso
from control_plane.auth.models import LoginAttemptWindow
from control_plane.database import async_session
from jose import JWTError
from sqlalchemy import select

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset_seed():
    """The auth router seeds a default admin once per process via a module flag;
    reset it so each test's fresh DB gets seeded on first /login."""
    import control_plane.auth.router as auth_router

    auth_router._seeded = False
    yield


# ─── JWT service ──────────────────────────────────────────────────────────

class TestJWTService:
    def test_hash_and_verify_password(self):
        h = service.hash_password("s3cret")
        assert h != "s3cret"
        assert service.verify_password("s3cret", h)
        assert not service.verify_password("wrong", h)

    def test_token_roundtrip(self):
        tok = service.create_access_token(7, "u@x.io", "operator")
        payload = service.decode_token(tok)
        assert payload["sub"] == "7"
        assert payload["email"] == "u@x.io"
        assert payload["role"] == "operator"

    def test_decode_rejects_tampered_token(self):
        tok = service.create_access_token(1, "a@b.io", "admin")
        with pytest.raises(JWTError):
            service.decode_token(tok + "tampered")


# ─── RBAC matrix ────────────────────────────────────────────────────────────

class TestRBAC:
    def test_admin_can_delete_users(self):
        assert rbac.check_permission("admin", "users:delete")

    def test_viewer_read_only(self):
        assert rbac.check_permission("viewer", "gateways:read")
        assert not rbac.check_permission("viewer", "gateways:write")

    def test_operator_cannot_touch_users(self):
        assert rbac.check_permission("operator", "gateways:write")
        assert not rbac.check_permission("operator", "users:delete")

    def test_unknown_role_has_no_permissions(self):
        assert not rbac.check_permission("ghost", "gateways:read")

    def test_oidc_role_mapping_requires_exact_group_or_scope(self):
        assert oidc._role_from_claims({"groups": ["admin"]}) == "admin"
        assert oidc._role_from_claims({"scope": "openid ostiari.operator"}) == "operator"
        assert oidc._role_from_claims({"groups": ["not-admins"]}) == "viewer"
        assert oidc._role_from_claims({"scope": "openid administrator.read"}) == "viewer"

    def test_browser_sso_role_mapping_handles_scalar_claims_exactly(self):
        assert sso.extract_roles_from_claims({"groups": "admins"}, "okta") == "admin"
        assert sso.extract_roles_from_claims({"groups": "not-admins"}, "okta") is None


# ─── Auth dependency (401 paths) ────────────────────────────────────────────

class TestAuthDependency:
    async def test_no_header_401(self, client):
        r = await client.get("/api/auth/me")
        assert r.status_code == 401

    async def test_malformed_header_401(self, client):
        r = await client.get("/api/auth/me", headers={"Authorization": "Token abc"})
        assert r.status_code == 401

    async def test_invalid_token_401(self, client):
        r = await client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
        assert r.status_code == 401


# ─── Login flow ─────────────────────────────────────────────────────────────

class TestLogin:
    async def test_login_default_admin_succeeds(self, client):
        # First login seeds the default admin@ostiari.ai / admin.
        r = await client.post("/api/auth/login", json={"email": "admin@ostiari.ai", "password": "admin"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["access_token"]
        assert body["user"]["role"] == "admin"

    async def test_login_wrong_password_401(self, client):
        r = await client.post("/api/auth/login", json={"email": "admin@ostiari.ai", "password": "nope"})
        assert r.status_code == 401

    async def test_login_unknown_user_401(self, client):
        r = await client.post("/api/auth/login", json={"email": "ghost@x.io", "password": "x"})
        assert r.status_code == 401

    async def test_login_is_durably_rate_limited_without_storing_email(
        self,
        client,
        monkeypatch,
    ):
        monkeypatch.setenv("OSTIARI_LOGIN_RATE_LIMIT", "true")
        monkeypatch.setenv("OSTIARI_LOGIN_ATTEMPTS_PER_MINUTE", "2")
        monkeypatch.setenv("OSTIARI_LOGIN_SOURCE_ATTEMPTS_PER_MINUTE", "100")

        statuses = []
        for _ in range(3):
            response = await client.post(
                "/api/auth/login",
                json={"email": "victim@example.com", "password": "wrong"},
            )
            statuses.append(response.status_code)

        assert statuses == [401, 401, 429]
        assert response.headers["Retry-After"]
        async with async_session() as db:
            rows = list(
                (await db.execute(select(LoginAttemptWindow))).scalars()
            )
        assert rows
        assert all("victim@example.com" not in row.key_digest for row in rows)

    async def test_successful_login_clears_account_failure_window(
        self,
        client,
        monkeypatch,
    ):
        monkeypatch.setenv("OSTIARI_LOGIN_RATE_LIMIT", "true")
        monkeypatch.setenv("OSTIARI_LOGIN_ATTEMPTS_PER_MINUTE", "2")
        monkeypatch.setenv("OSTIARI_LOGIN_SOURCE_ATTEMPTS_PER_MINUTE", "100")

        assert (
            await client.post(
                "/api/auth/login",
                json={"email": "admin@ostiari.ai", "password": "wrong"},
            )
        ).status_code == 401
        assert (
            await client.post(
                "/api/auth/login",
                json={"email": "admin@ostiari.ai", "password": "admin"},
            )
        ).status_code == 200

        statuses = []
        for _ in range(3):
            response = await client.post(
                "/api/auth/login",
                json={"email": "admin@ostiari.ai", "password": "wrong"},
            )
            statuses.append(response.status_code)
        assert statuses == [401, 401, 429]


# ─── User management (admin-gated) ──────────────────────────────────────────

class TestUserManagement:
    async def _admin_token(self, client):
        r = await client.post("/api/auth/login", json={"email": "admin@ostiari.ai", "password": "admin"})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    async def test_register_requires_admin(self, client, viewer_headers):
        r = await client.post("/api/auth/register",
                              json={"email": "n@x.io", "name": "N", "password": "pw", "role": "viewer"},
                              headers=viewer_headers)
        assert r.status_code == 403

    async def test_register_rejects_unknown_role(self, client):
        hdr = await self._admin_token(client)
        r = await client.post(
            "/api/auth/register",
            json={
                "email": "editor@x.io",
                "name": "Former Editor",
                "password": "pw",
                "role": "editor",
            },
            headers=hdr,
        )
        assert r.status_code == 422

    async def test_register_rejects_weak_password_in_production(
        self,
        client,
        monkeypatch,
    ):
        hdr = await self._admin_token(client)
        monkeypatch.setenv("OSTIARI_ENV", "production")
        r = await client.post(
            "/api/auth/register",
            json={
                "email": "weak@x.io",
                "name": "Weak",
                "password": "short",
                "role": "viewer",
            },
            headers=hdr,
        )
        assert r.status_code == 422
        assert "12 characters" in r.json()["detail"]

    async def test_register_and_list_and_delete(self, client):
        hdr = await self._admin_token(client)
        # register
        r = await client.post("/api/auth/register",
                              json={"email": "bob@x.io", "name": "Bob", "password": "pw", "role": "viewer"},
                              headers=hdr)
        assert r.status_code == 201, r.text
        new_id = r.json()["id"]
        # duplicate email -> 409
        r = await client.post("/api/auth/register",
                              json={"email": "bob@x.io", "name": "Bob2", "password": "pw", "role": "viewer"},
                              headers=hdr)
        assert r.status_code == 409
        # list includes admin + bob
        r = await client.get("/api/auth/users", headers=hdr)
        assert r.status_code == 200
        emails = {u["email"] for u in r.json()}
        assert "bob@x.io" in emails and "admin@ostiari.ai" in emails
        # delete bob
        r = await client.request("DELETE", f"/api/auth/users/{new_id}", headers=hdr)
        assert r.status_code == 204
        # delete missing -> 404
        r = await client.request("DELETE", "/api/auth/users/9999", headers=hdr)
        assert r.status_code == 404

    async def test_list_users_requires_auth(self, client):
        r = await client.get("/api/auth/users")
        assert r.status_code == 401


# ─── SSO ──────────────────────────────────────────────────────────────────

class TestSSO:
    async def test_sso_config_disabled_by_default(self, client):
        r = await client.get("/api/auth/sso/config")
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    async def test_sso_login_without_config(self, client):
        # With SSO unconfigured, login should not 500 — expect a 4xx/redirect, not crash.
        r = await client.get("/api/auth/sso/login", follow_redirects=False)
        assert r.status_code < 500
