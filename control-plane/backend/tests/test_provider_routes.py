"""Durable provider route configuration and delivery tests."""

from __future__ import annotations

import httpx
import pytest
from control_plane.auth.service import create_access_token
from control_plane.database import async_session
from control_plane.models.database import ProviderRouteRecord
from control_plane.routers.provider_routes import runtime_route_catalog
from sqlalchemy import select

pytestmark = pytest.mark.anyio


def _admin(org: str) -> dict[str, str]:
    token = create_access_token(
        user_id=1,
        email=f"admin@{org}.test",
        role="admin",
        org=org,
    )
    return {"Authorization": f"Bearer {token}"}


def _route(
    route_id: str = "openai:primary",
    *,
    api_key: str = "route-secret",
) -> dict:
    return {
        "route_id": route_id,
        "endpoint": "https://primary.openai.example",
        "credentials": {"api_key": api_key},
        "allowed_models": ["gpt-4o", "gpt-4o-mini"],
        "weight": 3,
        "priority": 0,
        "max_concurrency": 25,
        "capacity_group": "openai-account-a",
        "capacity_limit": 40,
        "max_connections": 50,
        "max_connections_per_host": 25,
        "keepalive_timeout": 45,
        "extra_headers": {"X-Private-Route": "header-secret"},
        "extra_params": {"proxy_url": "https://proxy-secret@example.test"},
    }


async def _add_provider(client, headers, name: str = "openai", **extra):
    return await client.post(
        "/api/providers",
        headers=headers,
        json={"name": name, "api_key": "legacy-secret", **extra},
    )


async def test_route_crud_encrypts_private_config_and_never_returns_it(
    client,
    admin_headers,
):
    await _add_provider(client, admin_headers)

    created = await client.post(
        "/api/providers/openai/routes",
        headers=admin_headers,
        json=_route(),
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["route_id"] == "openai:primary"
    assert body["provider"] == "openai"
    assert body["has_credentials"] is True
    assert body["has_custom_headers"] is True
    assert body["has_extra_params"] is True
    assert "credentials" not in body
    assert "route-secret" not in created.text

    async with async_session() as db:
        stored = (
            await db.execute(select(ProviderRouteRecord))
        ).scalar_one()
        assert "route-secret" not in stored.private_config_encrypted
        assert "header-secret" not in stored.private_config_encrypted
        runtime = await runtime_route_catalog(db, "default")
    assert runtime[0]["credentials"] == {"api_key": "route-secret"}
    assert runtime[0]["extra_headers"] == {
        "X-Private-Route": "header-secret"
    }

    updated = await client.put(
        "/api/providers/openai/routes/openai:primary",
        headers=admin_headers,
        json={
            "endpoint": "https://backup.openai.example",
            "credentials": {"api_key": "rotated-secret"},
            "weight": 1,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["endpoint"] == "https://backup.openai.example"
    async with async_session() as db:
        runtime = await runtime_route_catalog(db, "default")
    assert runtime[0]["credentials"] == {"api_key": "rotated-secret"}
    assert runtime[0]["extra_headers"] == {
        "X-Private-Route": "header-secret"
    }

    deleted = await client.delete(
        "/api/providers/openai/routes/openai:primary",
        headers=admin_headers,
    )
    assert deleted.status_code == 200
    assert (await client.get(
        "/api/providers/routes",
        headers=admin_headers,
    )).json() == []


async def test_explicit_disabled_route_suppresses_legacy_default(
    client,
    admin_headers,
):
    await _add_provider(client, admin_headers)
    async with async_session() as db:
        legacy = await runtime_route_catalog(db, "default")
    assert legacy[0]["route_id"] == "openai:default"
    assert legacy[0]["credentials"] == {"api_key": "legacy-secret"}

    created = await client.post(
        "/api/providers/openai/routes",
        headers=admin_headers,
        json={**_route(), "enabled": False},
    )
    assert created.status_code == 201

    async with async_session() as db:
        runtime = await runtime_route_catalog(db, "default")
    assert runtime == []


async def test_auth_type_change_without_new_credentials_clears_old_secret(
    client,
    admin_headers,
):
    await _add_provider(client, admin_headers)
    await client.post(
        "/api/providers/openai/routes",
        headers=admin_headers,
        json=_route(),
    )

    updated = await client.put(
        "/api/providers/openai/routes/openai:primary",
        headers=admin_headers,
        json={"auth_type": "aws_credentials"},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["has_credentials"] is False
    async with async_session() as db:
        runtime = await runtime_route_catalog(db, "default")
    assert runtime[0]["credentials"] == {}


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret@api.example",
        "https://api.example?token=secret",
        "https://api.example#secret",
    ],
)
async def test_route_endpoint_rejects_embedded_private_material(
    client,
    admin_headers,
    endpoint,
):
    await _add_provider(client, admin_headers)

    response = await client.post(
        "/api/providers/openai/routes",
        headers=admin_headers,
        json={**_route(), "endpoint": endpoint},
    )

    assert response.status_code == 422


@pytest.mark.usefixtures("multi_tenant_mode")
async def test_route_ids_and_secrets_are_tenant_isolated(client):
    org_a = _admin("org-a")
    org_b = _admin("org-b")
    await _add_provider(client, org_a)
    await _add_provider(client, org_b)

    assert (
        await client.post(
            "/api/providers/openai/routes",
            headers=org_a,
            json=_route(api_key="secret-a"),
        )
    ).status_code == 201
    assert (
        await client.post(
            "/api/providers/openai/routes",
            headers=org_b,
            json=_route(api_key="secret-b"),
        )
    ).status_code == 201

    assert len((await client.get(
        "/api/providers/routes",
        headers=org_a,
    )).json()) == 1
    assert len((await client.get(
        "/api/providers/routes",
        headers=org_b,
    )).json()) == 1
    async with async_session() as db:
        routes_a = await runtime_route_catalog(db, "org-a")
        routes_b = await runtime_route_catalog(db, "org-b")
    assert routes_a[0]["credentials"]["api_key"] == "secret-a"
    assert routes_b[0]["credentials"]["api_key"] == "secret-b"


async def test_deleting_provider_deletes_its_concrete_routes(
    client,
    admin_headers,
):
    await _add_provider(client, admin_headers)
    await client.post(
        "/api/providers/openai/routes",
        headers=admin_headers,
        json=_route(),
    )

    deleted = await client.delete(
        "/api/providers/openai",
        headers=admin_headers,
    )

    assert deleted.status_code == 200
    async with async_session() as db:
        records = (
            await db.execute(select(ProviderRouteRecord))
        ).scalars().all()
    assert records == []


async def test_push_sends_resolved_routes_only_to_gateway_config_channel(
    client,
    admin_headers,
    monkeypatch,
):
    calls: list[dict] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers", {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return httpx.Response(
                200,
                json={"modules_active": ["llm_gateway"]},
            )

        async def post(self, url, json=None, **kwargs):
            calls.append({
                "url": url,
                "json": json,
                "headers": self.headers,
            })
            return httpx.Response(200, json={"status": "applied"})

    from control_plane.routers import provider_routes

    monkeypatch.setattr(provider_routes.httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("OSTIARI_CONFIG_ADMIN_KEY", "config-admin-secret")
    await client.post(
        "/api/gateways",
        headers=admin_headers,
        json={
            "id": "gateway-1",
            "name": "gateway-1",
            "endpoint": "https://gateway.example",
        },
    )
    await _add_provider(client, admin_headers)
    await client.post(
        "/api/providers/openai/routes",
        headers=admin_headers,
        json=_route(),
    )

    pushed = await client.post(
        "/api/providers/routes/push",
        headers=admin_headers,
    )

    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["pushed"] == 1
    assert len(calls) == 1
    assert calls[0]["url"] == (
        "https://gateway.example/config/provider-routes"
    )
    assert calls[0]["headers"] == {
        "X-Config-Admin-Key": "config-admin-secret"
    }
    route = calls[0]["json"]["routes"][0]
    assert route["credentials"] == {"api_key": "route-secret"}
    assert route["endpoint"] == "https://primary.openai.example"
    assert "route-secret" not in pushed.text


async def test_config_bundle_exposes_route_secrets_only_to_machine(
    client,
    admin_headers,
    monkeypatch,
):
    monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
    monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "machine-secret")
    await client.post(
        "/api/gateways",
        headers=admin_headers,
        json={
            "id": "gateway-1",
            "name": "gateway-1",
            "endpoint": "https://gateway.example",
        },
    )
    await _add_provider(client, admin_headers)
    await client.post(
        "/api/providers/openai/routes",
        headers=admin_headers,
        json=_route(),
    )

    operator = await client.get(
        "/api/gateways/gateway-1/config-bundle",
        headers=admin_headers,
    )
    assert operator.status_code == 200, operator.text
    operator_route = operator.json()["provider_routes"][0]
    assert operator_route["has_credentials"] is True
    assert operator_route["has_custom_headers"] is True
    assert operator_route["has_extra_params"] is True
    assert "credentials" not in operator_route
    assert "extra_headers" not in operator_route
    assert "extra_params" not in operator_route
    assert "route-secret" not in operator.text
    assert "header-secret" not in operator.text
    assert "proxy-secret" not in operator.text

    machine = await client.get(
        "/api/gateways/gateway-1/config-bundle",
        headers={"X-Ostiari-Service-Key": "machine-secret"},
    )
    assert machine.status_code == 200, machine.text
    machine_route = machine.json()["provider_routes"][0]
    assert machine_route["credentials"] == {"api_key": "route-secret"}
    assert machine_route["extra_headers"] == {
        "X-Private-Route": "header-secret"
    }
    assert machine_route["extra_params"] == {
        "proxy_url": "https://proxy-secret@example.test"
    }


async def test_generic_push_rejects_plaintext_provider_routes(
    client,
    admin_headers,
):
    await client.post(
        "/api/gateways",
        headers=admin_headers,
        json={
            "id": "gateway-1",
            "name": "gateway-1",
            "endpoint": "https://gateway.example",
        },
    )

    response = await client.post(
        "/api/gateways/gateway-1/push-config",
        headers=admin_headers,
        json={"provider_routes": [_route()]},
    )

    assert response.status_code == 422
    assert "encrypted provider route API" in response.text
    assert "route-secret" not in response.text


async def test_route_write_requires_admin_and_valid_provider(client, viewer_headers):
    response = await client.post(
        "/api/providers/openai/routes",
        headers=viewer_headers,
        json=_route(),
    )
    assert response.status_code == 403

    admin = _admin("default")
    missing = await client.post(
        "/api/providers/openai/routes",
        headers=admin,
        json=_route(),
    )
    assert missing.status_code == 404


async def test_runtime_health_aggregation_is_secret_free(
    client,
    admin_headers,
    monkeypatch,
):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            return httpx.Response(200, json={
                "routes": [{
                    "route_id": "openai:primary",
                    "provider": "openai",
                    "status": "healthy",
                    "adaptive_weight": 0.82,
                    "inflight": 2,
                    "credentials": {"api_key": "must-not-escape"},
                    "api_key": "top-level-secret",
                    "unexpected_private_value": "must-not-escape-either",
                }]
            })

    from control_plane.routers import provider_routes

    monkeypatch.setattr(provider_routes.httpx, "AsyncClient", FakeClient)
    await client.post(
        "/api/gateways",
        headers=admin_headers,
        json={
            "id": "gateway-1",
            "name": "gateway-1",
            "endpoint": "https://gateway.example",
        },
    )

    response = await client.get(
        "/api/providers/routes/runtime",
        headers=admin_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] == 1
    route = body["snapshots"][0]["routes"][0]
    assert route["adaptive_weight"] == 0.82
    assert "credentials" not in route
    assert "api_key" not in route
    assert "unexpected_private_value" not in route
    assert "must-not-escape" not in response.text
