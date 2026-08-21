"""Per-gateway OIDC workload identity and binding contracts."""

from __future__ import annotations

import socket

import pytest
from control_plane.auth import workload

pytestmark = pytest.mark.anyio

ISSUER = "https://workload.test/issuer"


async def test_registration_binds_subject_and_allows_heartbeat(
    client,
    workload_signer,
):
    headers = workload_signer("gateway-a", subject="workload-a")

    registered = await client.post(
        "/api/gateways/gateway-a/register",
        json={},
        headers=headers,
    )
    heartbeat = await client.post(
        "/api/gateways/gateway-a/heartbeat",
        headers=headers,
    )

    assert registered.status_code == 200
    assert heartbeat.status_code == 200

    from control_plane.database import async_session
    from control_plane.models.database import Gateway

    async with async_session() as db:
        gateway = await db.get(
            Gateway,
            {"org_id": "default", "id": "gateway-a"},
        )
        assert gateway is not None
        assert gateway.workload_issuer == ISSUER
        assert gateway.workload_subject == "workload-a"


async def test_gateway_claim_cannot_address_another_gateway(
    client,
    workload_signer,
):
    response = await client.post(
        "/api/gateways/gateway-b/register",
        json={},
        headers=workload_signer("gateway-a"),
    )

    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"]


async def test_bound_gateway_rejects_different_subject(
    client,
    workload_signer,
):
    first = await client.post(
        "/api/gateways/gateway-a/register",
        json={},
        headers=workload_signer("gateway-a", subject="subject-one"),
    )
    second = await client.post(
        "/api/gateways/gateway-a/register",
        json={},
        headers=workload_signer("gateway-a", subject="subject-two"),
    )

    assert first.status_code == 200
    assert second.status_code == 403
    assert "different workload identity" in second.json()["detail"]


async def test_subject_cannot_bind_two_gateways(client, workload_signer):
    assert (
        await client.post(
            "/api/gateways/gateway-a/register",
            json={},
            headers=workload_signer("gateway-a", subject="shared-subject"),
        )
    ).status_code == 200

    response = await client.post(
        "/api/gateways/gateway-b/register",
        json={},
        headers=workload_signer("gateway-b", subject="shared-subject"),
    )

    assert response.status_code == 409
    assert "already bound" in response.json()["detail"]


async def test_subject_binding_supports_standard_oauth_tokens_without_gateway_claim(
    client,
    workload_signer,
):
    headers = workload_signer(
        "gateway-a",
        subject="oauth-client-subject",
        include_gateway_id=False,
    )

    assert (
        await client.post(
            "/api/gateways/gateway-a/register",
            json={},
            headers=headers,
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/gateways/gateway-a/heartbeat",
            headers=headers,
        )
    ).status_code == 200
    conflict = await client.post(
        "/api/gateways/gateway-b/register",
        json={},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert "already bound" in conflict.json()["detail"]


async def test_single_tenant_oauth_token_may_omit_tenant_claim(
    client,
    workload_signer,
    monkeypatch,
):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv("OSTIARI_TENANCY_MODE", "single")
    monkeypatch.setenv("OSTIARI_ORG_ID", "production-org")
    monkeypatch.setenv(
        "OSTIARI_GATEWAY_CALLBACK_ALLOW",
        "gateway.example",
    )
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("8.8.8.8", 0),
            )
        ],
    )

    response = await client.post(
        "/api/gateways/gateway-a/register",
        json={
            "org_id": "production-org",
            "callback_url": "https://gateway.example",
        },
        headers=workload_signer(
            "gateway-a",
            subject="oauth-client-subject",
            tenant_id=None,
            include_gateway_id=False,
        ),
    )

    assert response.status_code == 200, response.text

    from control_plane.database import async_session
    from control_plane.models.database import Gateway

    async with async_session() as db:
        gateway = await db.get(
            Gateway,
            {"org_id": "production-org", "id": "gateway-a"},
        )
        assert gateway is not None
        assert gateway.org_id == "production-org"


async def test_multi_tenant_oauth_token_requires_tenant_claim(
    client,
    workload_signer,
    monkeypatch,
):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv("OSTIARI_TENANCY_MODE", "multi")

    response = await client.post(
        "/api/gateways/gateway-a/register",
        json={},
        headers=workload_signer(
            "gateway-a",
            subject="oauth-client-subject",
            tenant_id=None,
            include_gateway_id=False,
        ),
    )

    assert response.status_code == 401
    assert "workload authentication required" in response.json()["detail"].lower()


async def test_body_gateway_id_must_match_token(client, workload_signer):
    assert (
        await client.post(
            "/api/gateways/gateway-a/register",
            json={},
            headers=workload_signer("gateway-a"),
        )
    ).status_code == 200

    response = await client.post(
        "/api/costs/record/batch",
        headers=workload_signer("gateway-a"),
        json=[
            {
                "gateway_id": "gateway-b",
                "agent_id": "agent",
                "model": "gpt-4o-mini",
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
            }
        ],
    )

    assert response.status_code == 403
    assert "not authorized" in response.json()["detail"]


async def test_trace_gateway_id_must_match_token(client, workload_signer):
    assert (
        await client.post(
            "/api/gateways/gateway-a/register",
            json={},
            headers=workload_signer("gateway-a"),
        )
    ).status_code == 200

    response = await client.post(
        "/api/traces/ingest",
        headers=workload_signer("gateway-a"),
        json={
            "trace_id": "trace-cross-gateway",
            "gateway_id": "gateway-b",
            "action": "tool.call",
            "tier": "allow",
        },
    )

    assert response.status_code == 403


async def test_gateway_spend_supports_bound_workload_and_operator(
    client,
    workload_signer,
    admin_headers,
):
    workload_headers = workload_signer("gateway-a")
    assert (
        await client.post(
            "/api/gateways/gateway-a/register",
            json={},
            headers=workload_headers,
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/gateways/gateway-a/spend",
            json={"spend": {"agent-a": 1.25}},
            headers=workload_headers,
        )
    ).status_code == 200

    workload_response = await client.get(
        "/api/gateways/gateway-a/spend",
        headers=workload_headers,
    )
    operator_response = await client.get(
        "/api/gateways/gateway-a/spend",
        headers=admin_headers,
    )

    assert workload_response.status_code == 200
    assert operator_response.status_code == 200
    assert workload_response.json() == operator_response.json()
    assert operator_response.json()["spend"] == {"agent-a": 1.25}


async def test_production_rejects_legacy_shared_key(client, monkeypatch):
    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
    monkeypatch.setenv("OSTIARI_SERVICE_TOKEN", "legacy-secret")
    monkeypatch.delenv("OSTIARI_WORKLOAD_OIDC_ISSUER", raising=False)

    response = await client.post(
        "/api/gateways/legacy/register",
        json={"callback_url": "https://gateway.example"},
        headers={"X-Ostiari-Service-Key": "legacy-secret"},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_workload_identity_provider_outage_is_retryable(
    client,
    monkeypatch,
):
    monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
    monkeypatch.setenv("OSTIARI_WORKLOAD_OIDC_ISSUER", ISSUER)
    monkeypatch.setattr(
        workload,
        "validate_workload_token",
        lambda _token: (_ for _ in ()).throw(
            workload.WorkloadOIDCUnavailableError("unavailable")
        ),
    )

    response = await client.post(
        "/api/gateways/gateway-a/register",
        json={},
        headers={"Authorization": "Bearer signed-token"},
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
