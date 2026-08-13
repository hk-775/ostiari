"""Durable control-plane runtime configuration."""

from __future__ import annotations

import asyncio

import pytest
from control_plane.database import async_session
from control_plane.models.database import RuntimeStateRecord
from control_plane.persistence import import_legacy_state, load_runtime_caches
from sqlalchemy import delete, select

pytestmark = pytest.mark.anyio


async def test_runtime_configuration_restores_after_cache_loss(
    client,
    admin_headers,
):
    assert (
        await client.post(
            "/api/agents",
            json={
                "name": "durable-agent",
                "framework": "openai",
                "gateway_id": "gw-durable",
            },
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/models",
            json={
                "name": "durable-model",
                "routing_strategy": "round-robin",
                "providers": [
                    {"provider": "openai", "model_id": "gpt-4o"}
                ],
            },
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/providers",
            headers=admin_headers,
            json={"name": "openai", "api_key": "persisted-secret"},
        )
    ).status_code == 200
    quota = await client.post(
        "/api/quotas",
        json={
            "name": "durable-quota",
            "scope": "gateway",
            "scope_id": "gw-durable",
            "rate_limit_rpm": 25,
        },
    )
    assert quota.status_code == 200
    assert (
        await client.post(
            "/api/roi/cost-model",
            json={"entries": [{"pattern": "delete", "cost": 10.0}], "fallback": 2.0},
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/token-broker/config",
            json={"bulk_discount": 0.2, "markup": 0.1},
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/payments/pricing?gateway_id=gw-durable",
            json={"mode": "metered", "default": 0.01, "overrides": {}},
        )
    ).status_code == 200

    from control_plane.routers.agents import _agents
    from control_plane.routers.model_config import _models
    from control_plane.routers.payments import _pricing
    from control_plane.routers.providers import _providers
    from control_plane.routers.quotas import _quotas
    from control_plane.routers.roi import _cost_model
    from control_plane.routers.token_broker import _config

    _agents.clear()
    _models.clear()
    _providers.clear()
    _quotas.clear()
    _pricing.clear()
    _cost_model.clear()
    _config.clear()

    async with async_session() as db:
        await load_runtime_caches(db)

    assert _agents["default"]["durable-agent"].framework == "openai"
    assert _models["default"]["durable-model"].providers[0].model_id == "gpt-4o"
    assert _providers["default"]["openai"].api_key_encrypted
    assert next(iter(_quotas["default"].values())).rate_limit_rpm == 25
    assert _cost_model["default"]["fallback"] == 2.0
    assert _config["default"]["markup"] == 0.1
    assert _pricing["default"]["gw-durable"]["default"] == 0.01

    async with async_session() as db:
        provider_row = (
            await db.execute(
                select(RuntimeStateRecord).where(
                    RuntimeStateRecord.namespace == "providers",
                    RuntimeStateRecord.item_key == "openai",
                )
            )
        ).scalar_one()
    assert "persisted-secret" not in str(provider_row.value)


async def test_concurrent_quota_ids_are_unique(client):
    async def create(index: int) -> int:
        response = await client.post(
            "/api/quotas",
            json={
                "name": f"quota-{index}",
                "scope": "gateway",
                "scope_id": "gw",
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["id"]

    ids = await asyncio.gather(*(create(index) for index in range(20)))
    assert len(set(ids)) == 20


async def test_offline_config_queue_survives_process_memory(client):
    created = await client.post(
        "/api/gateways",
        json={
            "id": "gw-offline",
            "name": "Offline",
            "endpoint": "http://127.0.0.1:1",
        },
    )
    assert created.status_code == 200

    queued = await client.post(
        "/api/gateways/gw-offline/push-config",
        json={"mode": "shadow"},
    )
    assert queued.status_code == 200
    assert queued.json()["status"] == "queued"

    heartbeat = await client.post("/api/gateways/gw-offline/heartbeat")
    assert heartbeat.status_code == 200
    assert {"mode": "shadow"} in heartbeat.json()["config_updates"]

    second = await client.post("/api/gateways/gw-offline/heartbeat")
    assert second.status_code == 200
    assert {"mode": "shadow"} not in second.json()["config_updates"]


async def test_legacy_state_import_marker_prevents_reimport():
    state = {
        "agents": [
            {
                "name": "legacy-agent",
                "framework": "openai",
                "gateway_id": "legacy-gateway",
            }
        ]
    }
    async with async_session() as db:
        assert await import_legacy_state(db, state) is True
        await db.execute(
            delete(RuntimeStateRecord).where(
                RuntimeStateRecord.namespace != "_migration"
            )
        )
        await db.commit()

    async with async_session() as db:
        assert await import_legacy_state(db, state) is False
        rows = list((await db.execute(select(RuntimeStateRecord))).scalars())

    assert [(row.namespace, row.item_key) for row in rows] == [
        ("_migration", "legacy_state_import")
    ]
