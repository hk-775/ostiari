"""Browser OIDC flow from IdP callback through the frontend session."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from control_plane.auth import sso, sso_router

pytestmark = pytest.mark.anyio


def _configure(monkeypatch, *, frontend: str = "https://dashboard.example.com"):
    monkeypatch.setenv(
        "OIDC_ISSUER",
        "https://login.microsoftonline.com/example/v2.0",
    )
    monkeypatch.setenv("OIDC_CLIENT_ID", "client-id")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("OSTIARI_FRONTEND_URL", frontend)


class TestSSOBrowserFlow:
    async def test_config_and_login_expose_browser_flow(
        self, client, monkeypatch
    ):
        _configure(monkeypatch)

        async def authorization_url(config):
            return (
                "https://idp.example.com/authorize?client_id=client-id",
                "state-123",
                "nonce-123",
            )

        monkeypatch.setattr(sso_router, "get_authorization_url", authorization_url)

        config = await client.get("/api/auth/sso/config")
        assert config.status_code == 200
        assert config.json() == {
            "enabled": True,
            "provider": "azure_ad",
            "login_url": "/api/auth/sso/login",
        }

        login = await client.get(
            "/api/auth/sso/login",
            follow_redirects=False,
        )
        assert login.status_code == 302
        assert login.headers["location"].startswith(
            "https://idp.example.com/authorize"
        )
        assert sso_router._pending_states["state-123"] == {
            "nonce": "nonce-123"
        }

    async def test_default_urls_match_local_backend_and_frontend(
        self, client, monkeypatch
    ):
        _configure(monkeypatch)
        monkeypatch.delenv("OIDC_REDIRECT_URI", raising=False)
        monkeypatch.delenv("OSTIARI_FRONTEND_URL", raising=False)

        config = sso.get_oidc_config()
        assert config is not None
        assert config.redirect_uri == "http://localhost:8400/api/auth/sso/callback"

        response = await client.get(
            "/api/auth/sso/callback",
            params={
                "error": "access_denied",
                "error_description": "Access denied & retry",
            },
            follow_redirects=False,
        )
        redirect = urlsplit(response.headers["location"])
        assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == (
            "http://localhost:9000/login"
        )
        assert parse_qs(redirect.query) == {
            "error": ["sso_failed"],
            "detail": ["Access denied & retry"],
        }

    async def test_callback_provisions_user_and_redirects_token_in_fragment(
        self, client, monkeypatch
    ):
        _configure(monkeypatch)
        seen: dict[str, str] = {}

        async def exchange_code(config, code):
            seen["code"] = code
            return {"access_token": "idp-access", "id_token": "idp-id-token"}

        async def validate_id_token(config, token, nonce=None):
            seen["nonce"] = nonce or ""
            return {
                "sub": "subject-123",
                "email": "operator@example.com",
                "name": "Example Operator",
                "roles": ["operators"],
            }

        async def get_userinfo(config, access_token):
            seen["access_token"] = access_token
            return {}

        monkeypatch.setattr(sso_router, "exchange_code", exchange_code)
        monkeypatch.setattr(sso_router, "validate_id_token", validate_id_token)
        monkeypatch.setattr(sso_router, "get_userinfo", get_userinfo)
        sso_router._pending_states["state-123"] = {"nonce": "nonce-123"}

        response = await client.get(
            "/api/auth/sso/callback",
            params={"code": "code-123", "state": "state-123"},
            follow_redirects=False,
        )

        assert response.status_code == 302
        redirect = urlsplit(response.headers["location"])
        assert f"{redirect.scheme}://{redirect.netloc}{redirect.path}" == (
            "https://dashboard.example.com/auth/sso-callback"
        )
        assert redirect.query == ""
        token = parse_qs(redirect.fragment)["token"][0]
        assert token
        assert seen == {
            "code": "code-123",
            "nonce": "nonce-123",
            "access_token": "idp-access",
        }

        me = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 200, me.text
        assert me.json() == {
            "id": 1,
            "email": "operator@example.com",
            "name": "Example Operator",
            "role": "operator",
        }

        password_login = await client.post(
            "/api/auth/login",
            json={"email": "operator@example.com", "password": "not-a-password"},
        )
        assert password_login.status_code == 401

    async def test_callback_rejects_disabled_existing_user(
        self, client, monkeypatch
    ):
        from control_plane.auth.models import User
        from control_plane.database import async_session
        from control_plane.models.database import DEFAULT_ORG, Organization

        _configure(monkeypatch)
        async with async_session() as db:
            db.add(Organization(id=DEFAULT_ORG, name="Default Organization"))
            db.add(
                User(
                    email="disabled@example.com",
                    name="Disabled",
                    hashed_password="",
                    role="viewer",
                    is_active=False,
                    org_id=DEFAULT_ORG,
                )
            )
            await db.commit()

        async def exchange_code(config, code):
            return {"id_token": "idp-id-token"}

        async def validate_id_token(config, token, nonce=None):
            return {
                "sub": "disabled-subject",
                "email": "disabled@example.com",
            }

        monkeypatch.setattr(sso_router, "exchange_code", exchange_code)
        monkeypatch.setattr(sso_router, "validate_id_token", validate_id_token)
        sso_router._pending_states["disabled-state"] = {"nonce": "nonce"}

        response = await client.get(
            "/api/auth/sso/callback",
            params={"code": "code", "state": "disabled-state"},
            follow_redirects=False,
        )

        redirect = urlsplit(response.headers["location"])
        assert redirect.path == "/login"
        assert parse_qs(redirect.query) == {"error": ["account_disabled"]}
