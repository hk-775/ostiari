"""Credential isolation and bounded downstream behavior for the UI proxy."""

from __future__ import annotations

import pytest

from ostiari.bounded_http import BoundedResponse, ResponseTooLargeError

pytestmark = pytest.mark.anyio


async def _gateway(client):
    response = await client.post(
        "/api/gateways",
        json={
            "id": "gw-proxy",
            "name": "Proxy gateway",
            "endpoint": "https://gateway.internal",
            "description": "",
        },
    )
    assert response.status_code == 200, response.text


async def test_operator_credentials_are_not_forwarded(
    client, monkeypatch, admin_headers
):
    from control_plane.routers import proxy

    await _gateway(client)
    monkeypatch.setenv("OSTIARI_GATEWAY_AGENT_TOKEN", "gateway-agent-token")
    monkeypatch.setenv("OSTIARI_GATEWAY_AGENT_ID", "dashboard-service")
    captured = {}

    async def fake_request(client, method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return BoundedResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            content=b'{"ok":true}',
        )

    monkeypatch.setattr(proxy, "request_limited", fake_request)
    response = await client.post(
        "/api/proxy/gateway/gw-proxy/tool/search?q=one",
        headers={
            **admin_headers,
            "Cookie": "session=secret",
            "X-Secret": "do-not-forward",
            "Traceparent": "00-abc-def-01",
        },
        json={"query": "two"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert captured["url"] == "https://gateway.internal/tool/search?q=one"
    assert captured["headers"]["Authorization"] == "Bearer gateway-agent-token"
    assert captured["headers"]["X-Agent-Id"] == "dashboard-service"
    assert captured["headers"]["traceparent"] == "00-abc-def-01"
    assert "Cookie" not in captured["headers"]
    assert "X-Secret" not in captured["headers"]
    assert admin_headers["Authorization"] not in captured["headers"].values()


async def test_config_proxy_uses_admin_key_not_agent_token(
    client, monkeypatch, admin_headers
):
    from control_plane.routers import proxy

    await _gateway(client)
    monkeypatch.setenv("OSTIARI_CONFIG_ADMIN_KEY", "config-admin")
    monkeypatch.setenv("OSTIARI_GATEWAY_AGENT_TOKEN", "gateway-agent-token")
    captured = {}

    async def fake_request(client, method, url, **kwargs):
        captured.update(kwargs)
        return BoundedResponse(200, {"content-type": "application/json"}, b"{}")

    monkeypatch.setattr(proxy, "request_limited", fake_request)
    response = await client.get(
        "/api/proxy/gateway/gw-proxy/config",
        headers=admin_headers,
    )

    assert response.status_code == 200
    assert captured["headers"]["X-Config-Admin-Key"] == "config-admin"
    assert "Authorization" not in captured["headers"]


async def test_downstream_401_is_not_a_control_plane_401(
    client, monkeypatch, admin_headers
):
    from control_plane.routers import proxy

    await _gateway(client)

    async def fake_request(client, method, url, **kwargs):
        return BoundedResponse(
            401,
            {"content-type": "application/json"},
            b'{"error":"bad gateway token"}',
        )

    monkeypatch.setattr(proxy, "request_limited", fake_request)
    response = await client.get(
        "/api/proxy/gateway/gw-proxy/tools",
        headers=admin_headers,
    )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "Gateway rejected control-plane credentials",
        "gateway_status": 401,
    }


async def test_oversized_gateway_response_is_502(
    client, monkeypatch, admin_headers
):
    from control_plane.routers import proxy

    await _gateway(client)
    monkeypatch.setenv("OSTIARI_PROXY_MAX_RESPONSE_BYTES", "1024")

    async def fake_request(client, method, url, **kwargs):
        raise ResponseTooLargeError(1024)

    monkeypatch.setattr(proxy, "request_limited", fake_request)
    response = await client.get(
        "/api/proxy/gateway/gw-proxy/tools",
        headers=admin_headers,
    )

    assert response.status_code == 502
    assert "1024 byte limit" in response.json()["detail"]
