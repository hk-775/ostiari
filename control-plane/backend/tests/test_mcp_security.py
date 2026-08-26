"""Security boundaries for control-plane MCP configuration."""

from __future__ import annotations

import pytest
from control_plane.auth.service import create_access_token
from sqlalchemy import select

pytestmark = pytest.mark.anyio


def _operator_headers() -> dict[str, str]:
    token = create_access_token(
        user_id=20,
        email="operator@test.io",
        role="operator",
    )
    return {"Authorization": f"Bearer {token}"}


async def _make_gateway(client, headers: dict[str, str]) -> None:
    response = await client.post(
        "/api/gateways",
        headers=headers,
        json={
            "id": "secure-gateway",
            "name": "Secure Gateway",
            "endpoint": "https://gateway.example",
        },
    )
    assert response.status_code == 200, response.text


async def test_mcp_config_is_encrypted_and_write_only(
    client,
    admin_headers,
    monkeypatch,
):
    await _make_gateway(client, admin_headers)
    secret = "mcp-private-value"

    created = await client.post(
        "/api/mcp-servers/secure-gateway",
        headers=admin_headers,
        json={
            "name": "remote-tools",
            "mode": "remote",
            "url": "https://mcp.example/mcp",
            "config": {"authorization": secret},
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["config"] == {}
    assert created.json()["has_config"] is True
    assert secret not in created.text

    from control_plane.database import async_session
    from control_plane.models.database import Gateway, McpServer

    async with async_session() as db:
        record = (await db.execute(select(McpServer))).scalar_one()
        assert record.config == {}
        assert record.config_encrypted
        assert secret not in record.config_encrypted
        gateway = await db.get(
            Gateway,
            {"org_id": "default", "id": "secure-gateway"},
        )
        gateway.config = {
            "custom_runtime": {
                "api_key": "gateway-bundle-secret",
                "max_tokens": 1024,
            }
        }
        await db.commit()

    human_bundle = await client.get(
        "/api/gateways/secure-gateway/config-bundle",
        headers=admin_headers,
    )
    assert human_bundle.status_code == 200, human_bundle.text
    assert human_bundle.json()["mcp_servers"][0]["config"] == {}
    assert human_bundle.json()["custom_runtime"] == {
        "api_key": "<redacted>",
        "max_tokens": 1024,
    }
    assert secret not in human_bundle.text
    assert "gateway-bundle-secret" not in human_bundle.text

    monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
    monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "machine-secret")
    machine_bundle = await client.get(
        "/api/gateways/secure-gateway/config-bundle",
        headers={"X-Ostiari-Service-Key": "machine-secret"},
    )
    assert machine_bundle.status_code == 200, machine_bundle.text
    assert machine_bundle.json()["mcp_servers"][0]["config"] == {
        "authorization": secret
    }
    assert machine_bundle.json()["custom_runtime"]["api_key"] == (
        "gateway-bundle-secret"
    )


async def test_mcp_execution_configuration_requires_admin(
    client,
    admin_headers,
):
    await _make_gateway(client, admin_headers)
    response = await client.post(
        "/api/mcp-servers/secure-gateway",
        headers=_operator_headers(),
        json={
            "name": "local-code",
            "mode": "stdio",
            "command": ["python", "-m", "server"],
        },
    )
    assert response.status_code == 403


async def test_remote_mcp_url_cannot_embed_credentials(
    client,
    admin_headers,
):
    await _make_gateway(client, admin_headers)
    response = await client.post(
        "/api/mcp-servers/secure-gateway",
        headers=admin_headers,
        json={
            "name": "credential-url",
            "mode": "remote",
            "url": "https://user:password@mcp.example/mcp?token=secret",
        },
    )
    assert response.status_code == 400
    assert "password" not in response.text
    assert "token=secret" not in response.text


async def test_generic_push_requires_admin_and_rejects_managed_secrets(
    client,
    admin_headers,
):
    await _make_gateway(client, admin_headers)

    operator = await client.post(
        "/api/gateways/secure-gateway/push-config",
        headers=_operator_headers(),
        json={"mode": "shadow"},
    )
    assert operator.status_code == 403

    secret = "must-not-be-queued"
    managed = await client.post(
        "/api/gateways/secure-gateway/push-config",
        headers=admin_headers,
        json={
            "mcp_servers": [
                {"name": "bad", "config": {"token": secret}}
            ]
        },
    )
    assert managed.status_code == 422
    assert "dedicated credential-safe APIs" in managed.text
    assert secret not in managed.text
