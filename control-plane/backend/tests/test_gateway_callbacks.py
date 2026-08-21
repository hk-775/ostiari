"""Gateway callback validation and registration SSRF boundaries."""

from __future__ import annotations

import socket

import pytest
from control_plane.services.gateway_callbacks import (
    GatewayCallbackError,
    validate_gateway_callback,
)

pytestmark = pytest.mark.anyio


def _dns(address: str):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            (address, 0),
        )
    ]


def test_callback_always_blocks_metadata(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args: _dns("169.254.169.254"))
    with pytest.raises(GatewayCallbackError, match="metadata"):
        validate_gateway_callback("http://metadata.internal:8421")


@pytest.mark.parametrize("value", ["", None])
def test_callback_rejects_empty_or_non_string_values(value):
    with pytest.raises(GatewayCallbackError, match="non-empty"):
        validate_gateway_callback(value)  # type: ignore[arg-type]


def test_production_callback_requires_allowed_destination(monkeypatch):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv("OSTIARI_GATEWAY_CALLBACK_ALLOW", "10.20.0.0/16")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args: _dns("10.30.1.2"))
    with pytest.raises(GatewayCallbackError, match="not in"):
        validate_gateway_callback("http://gateway.internal:8421")


def test_production_callback_accepts_allowed_destination(monkeypatch):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv("OSTIARI_GATEWAY_CALLBACK_ALLOW", "10.20.0.0/16")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args: _dns("10.20.1.2"))
    assert (
        validate_gateway_callback("http://gateway.internal:8421/")
        == "http://gateway.internal:8421"
    )


async def test_machine_registration_rejects_unadvertised_production_callback(
    client,
    monkeypatch,
    workload_signer,
):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv("OSTIARI_TENANCY_MODE", "single")
    monkeypatch.setenv("OSTIARI_ORG_ID", "production-org")
    response = await client.post(
        "/api/gateways/gw/register",
        json={"org_id": "production-org"},
        headers=workload_signer("gw", tenant_id="production-org"),
    )
    assert response.status_code == 422


async def test_gateway_patch_rejects_null_endpoint(client):
    created = await client.post(
        "/api/gateways",
        json={
            "id": "gw",
            "name": "Gateway",
            "endpoint": "http://localhost:8421",
        },
    )
    assert created.status_code == 200
    response = await client.patch("/api/gateways/gw", json={"endpoint": None})
    assert response.status_code == 422
