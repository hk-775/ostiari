"""End-to-end control-plane coverage for persisted per-agent quotas."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.anyio


async def _gateway(client, gateway_id: str = "gw-agent") -> None:
    response = await client.post("/api/gateways", json={
        "id": gateway_id,
        "name": "Agent Gateway",
        "endpoint": "http://gateway.local",
        "description": "",
    })
    assert response.status_code == 200, response.text


async def _agent(client, name: str, gateway_id: str = "gw-agent") -> None:
    response = await client.post("/api/agents", json={
        "name": name,
        "framework": "test",
        "gateway_id": gateway_id,
        "tools": ["search"],
    })
    assert response.status_code == 200, response.text


async def _quota(client, agent_id: str, **overrides):
    body = {
        "name": f"{agent_id} quota",
        "scope": "agent",
        "scope_id": agent_id,
        "rate_limit_rpm": 12,
        "budget_limit_usd": 5.0,
        "max_tokens_per_request": 2048,
        "allowed_models": ["gpt-4o-mini"],
        "allowed_providers": ["openai"],
        "alert_threshold_pct": 80,
        **overrides,
    }
    response = await client.post("/api/quotas", json=body)
    assert response.status_code == 200, response.text
    return response.json()


class TestAgentQuotaPersistence:
    async def test_registered_agent_resolves_gateway_and_all_fields_persist(self, client):
        await _gateway(client)
        await _agent(client, "research")

        quota = await _quota(client, "research")
        assert quota["gateway_id"] == "gw-agent"
        assert quota["allowed_providers"] == ["openai"]
        assert quota["alert_threshold_pct"] == 80

        listed = (await client.get("/api/quotas?scope=agent")).json()
        assert listed == [quota]
        assert (await client.get("/api/quotas?scope=gateway")).json() == []

    async def test_agent_quota_requires_gateway_and_is_unique(self, client):
        missing = await client.post("/api/quotas", json={
            "name": "orphan",
            "scope": "agent",
            "scope_id": "unknown",
        })
        assert missing.status_code == 422

        await _gateway(client)
        await _agent(client, "research")
        await _quota(client, "research")
        duplicate = await client.post("/api/quotas", json={
            "name": "duplicate",
            "scope": "agent",
            "scope_id": "research",
            "gateway_id": "gw-agent",
        })
        assert duplicate.status_code == 409

    async def test_list_reports_actual_agent_spend_and_recent_rpm(self, client):
        await _gateway(client)
        await _agent(client, "research")
        await _quota(client, "research")
        for cost in (0.25, 0.75):
            response = await client.post("/api/costs/record", json={
                "gateway_id": "gw-agent",
                "agent_id": "research",
                "model": "gpt-4o-mini",
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "cost_usd": cost,
            })
            assert response.status_code == 200, response.text

        quota = (await client.get("/api/quotas?scope=agent")).json()[0]
        assert quota["current_spend"] == 1.0
        assert quota["current_rpm"] == 2


class TestAgentQuotaPush:
    async def test_push_sends_complete_gateway_map_and_persists_bundle(
        self, client, monkeypatch
    ):
        await _gateway(client)
        await _agent(client, "research")
        await _agent(client, "ops")
        first = await _quota(client, "research")
        await _quota(
            client,
            "ops",
            rate_limit_rpm=30,
            max_tokens_per_request=4096,
            allowed_models=["*"],
            allowed_providers=["bedrock"],
            alert_threshold_pct=90,
        )
        await client.post("/api/costs/record", json={
            "gateway_id": "gw-agent",
            "agent_id": "research",
            "model": "gpt-4o-mini",
            "total_tokens": 10,
            "cost_usd": 1.25,
        })

        sent = {}

        class _Response:
            status_code = 200
            text = ""

            def json(self):
                return {"status": "applied"}

        class _Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, json=None):
                sent["url"] = url
                sent["json"] = json
                return _Response()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        response = await client.post(f"/api/quotas/{first['id']}/push")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "pushed"

        payload = sent["json"]
        assert sent["url"] == "http://gateway.local/config/agent-auth"
        assert payload["enabled"] is False
        assert payload["quota_enabled"] is True
        assert payload["default_grants"] == []
        assert set(payload["agents"]) == {"research", "ops"}
        research = payload["agents"]["research"]
        assert research == {
            "allowed_tools": [],
            "allowed_models": ["gpt-4o-mini"],
            "allowed_providers": ["openai"],
            "budget_usd": 5.0,
            "spend_usd": 1.25,
            "rate_limit_rpm": 12,
            "max_tokens_per_request": 2048,
            "alert_threshold_pct": 80,
            "description": "research quota",
        }

        from control_plane.database import async_session
        from control_plane.models.database import Gateway

        async with async_session() as db:
            gateway = await db.get(
                Gateway,
                {"org_id": "default", "id": "gw-agent"},
            )
            assert gateway.config["agent_auth"] == payload
        bundle = (await client.get("/api/gateways/gw-agent/config-bundle")).json()
        assert bundle["agent_auth"] == payload

    async def test_push_preserves_existing_tool_grants(self, client, monkeypatch):
        await _gateway(client)
        await _agent(client, "research")
        quota = await _quota(client, "research")

        from control_plane.database import async_session
        from control_plane.models.database import Gateway

        base = {
            "enabled": True,
            "default_grants": [],
            "default_models": ["*"],
            "default_providers": ["*"],
            "agents": {
                "research": {
                    "allowed_tools": ["search"],
                    "description": "least privilege",
                },
                "tool-only": {
                    "allowed_tools": ["lookup"],
                },
            },
        }
        async with async_session() as db:
            gateway = await db.get(
                Gateway,
                {"org_id": "default", "id": "gw-agent"},
            )
            gateway.config = {"agent_auth": base}
            await db.commit()

        sent = {}

        class _Response:
            status_code = 200
            text = ""

            def json(self):
                return {"status": "applied"}

        class _Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, _url, json=None):
                sent["json"] = json
                return _Response()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        response = await client.post(f"/api/quotas/{quota['id']}/push")
        assert response.status_code == 200
        assert sent["json"]["enabled"] is True
        assert sent["json"]["quota_enabled"] is True
        assert sent["json"]["default_grants"] == []
        assert sent["json"]["agents"]["research"]["allowed_tools"] == ["search"]
        assert sent["json"]["agents"]["tool-only"] == {"allowed_tools": ["lookup"]}

        async with async_session() as db:
            gateway = await db.get(
                Gateway,
                {"org_id": "default", "id": "gw-agent"},
            )
            assert gateway.config["agent_auth_base"] == base

    async def test_bulk_push_disables_policy_after_last_quota_is_deleted(
        self, client, monkeypatch
    ):
        await _gateway(client)
        await _agent(client, "research")
        quota = await _quota(client, "research")
        deleted = await client.delete(f"/api/quotas/{quota['id']}")
        assert deleted.json()["gateway_id"] == "gw-agent"

        sent = {}

        class _Response:
            status_code = 200
            text = ""

            def json(self):
                return {"status": "applied"}

        class _Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, _url, json=None):
                sent["json"] = json
                return _Response()

        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        response = await client.post("/api/quotas/agents/push?gateway_id=gw-agent")
        assert response.status_code == 200
        assert sent["json"]["enabled"] is False
        assert sent["json"]["quota_enabled"] is False
        assert sent["json"]["agents"] == {}

    async def test_spend_snapshots_merge_without_rolling_back(self, client):
        await _gateway(client)

        first = await client.post("/api/gateways/gw-agent/spend", json={
            "spend": {"research": 4.0, "ops": 1.0},
        })
        assert first.status_code == 200
        second = await client.post("/api/gateways/gw-agent/spend", json={
            "spend": {"research": 2.0, "ops": 1.5},
        })
        assert second.status_code == 200

        snapshot = (await client.get("/api/gateways/gw-agent/spend")).json()["spend"]
        assert snapshot == {"research": 4.0, "ops": 1.5}

    async def test_explicit_period_reset_replaces_snapshot(self, client):
        await _gateway(client)
        await client.post("/api/gateways/gw-agent/spend", json={
            "spend": {"research": 4.0, "ops": 1.0},
        })
        reset_at = datetime.now(timezone.utc).isoformat()
        response = await client.post("/api/gateways/gw-agent/spend", json={
            "spend": {"research": 0.0, "ops": 0.0},
            "reset": True,
            "reset_at": reset_at,
        })
        assert response.status_code == 200
        assert response.json()["reset"] is True
        snapshot = (await client.get("/api/gateways/gw-agent/spend")).json()["spend"]
        assert snapshot == {"research": 0.0, "ops": 0.0}
        bundle = (await client.get("/api/gateways/gw-agent/config-bundle")).json()
        assert bundle["budget_reset"]["last_reset_at"] == reset_at

    async def test_usage_before_reset_epoch_is_not_current_period_spend(self, client):
        await _gateway(client)
        await _agent(client, "research")
        await _quota(client, "research")
        response = await client.post("/api/costs/record", json={
            "gateway_id": "gw-agent",
            "agent_id": "research",
            "model": "gpt-4o-mini",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "cost_usd": 2.0,
        })
        assert response.status_code == 200
        await client.post("/api/gateways/gw-agent/spend", json={
            "spend": {"research": 0.0},
            "reset": True,
            "reset_at": datetime.now(timezone.utc).isoformat(),
        })

        quota = (await client.get("/api/quotas?scope=agent")).json()[0]
        assert quota["current_spend"] == 0.0


class TestAgentBudgetAlerts:
    async def test_alert_retains_agent_identity(self, client):
        await _gateway(client)
        response = await client.post("/api/quotas/alerts", json={
            "gateway_id": "gw-agent",
            "agent_id": "research",
            "threshold": "80%",
            "spend_usd": 4.0,
            "budget_usd": 5.0,
        })
        assert response.status_code == 200
        alert = (await client.get("/api/quotas/alerts")).json()[0]
        assert alert["agent_id"] == "research"
