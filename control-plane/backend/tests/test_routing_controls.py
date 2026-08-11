"""Tests for durable runtime routing controls and model-registry delivery."""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.anyio


async def _make_gateway(client, gateway_id: str = "gw1"):
    return await client.post("/api/gateways", json={
        "id": gateway_id,
        "name": gateway_id,
        "endpoint": "http://gateway.test",
        "description": "",
    })


@pytest.fixture
def outbound(monkeypatch):
    from control_plane.routers import model_config, routing_controls

    calls: list[dict] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

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
            calls.append({"url": url, "json": json})
            return httpx.Response(200, json={"status": "applied"})

    monkeypatch.setattr(routing_controls.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(model_config.httpx, "AsyncClient", FakeClient)
    return calls


class TestRoutingControls:
    async def test_task_rules_are_persisted_and_pushed(self, client, outbound):
        await _make_gateway(client)
        await client.post("/api/models", json={
            "name": "code-model",
            "routing_strategy": "round-robin",
            "providers": [{"provider": "openai", "model_id": "gpt-4o"}],
        })

        response = await client.put(
            "/api/routing-controls/gw1/task-classification",
            json={
                "rules": {"Coding Tasks": [" Code ", "function", "code"]},
                "model_mapping": {"Coding Tasks": "code-model"},
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["pushed"] is True
        assert outbound[-1] == {
            "url": "http://gateway.test/config/task-classification",
            "json": {
                "rules": {"coding_tasks": ["code", "function"]},
                "model_mapping": {"coding_tasks": "code-model"},
            },
        }
        stored = (await client.get("/api/routing-controls/gw1")).json()
        assert stored["task_classification"] == outbound[-1]["json"]

    async def test_unknown_task_target_is_rejected(self, client, outbound):
        await _make_gateway(client)
        response = await client.put(
            "/api/routing-controls/gw1/task-classification",
            json={
                "rules": {"coding": ["code"]},
                "model_mapping": {"coding": "missing"},
            },
        )
        assert response.status_code == 422
        assert outbound == []

    async def test_budget_schedule_has_durable_baseline(self, client, outbound):
        await _make_gateway(client)
        response = await client.put(
            "/api/routing-controls/gw1/budget-reset",
            json={"schedule": "weekly"},
        )
        assert response.status_code == 200
        config = response.json()["config"]
        assert config["schedule"] == "weekly"
        assert config["configured_at"]
        assert config["last_reset_at"] is None
        assert config["next_reset"]
        assert outbound[-1]["url"] == "http://gateway.test/config/budget-reset"

    async def test_manual_reset_calls_live_gateway(self, client, outbound):
        await _make_gateway(client)
        await client.post(
            "/api/gateways/gw1/spend",
            json={"spend": {"agent-1": 4.5}},
        )
        response = await client.post("/api/routing-controls/gw1/reset-spend")
        assert response.status_code == 200
        assert outbound[-1]["url"] == "http://gateway.test/config/quota/reset-spend"
        assert response.json()["last_reset_at"]
        spend = (await client.get("/api/gateways/gw1/spend")).json()
        assert spend == {"spend": {"agent-1": 0.0}}
        controls = (await client.get("/api/routing-controls/gw1")).json()
        assert controls["budget_reset"]["last_reset_at"]


class TestRuntimeModelRegistry:
    async def test_push_translates_registry_to_axon_contract(self, client, outbound):
        await _make_gateway(client)
        await client.post("/api/models", json={
            "name": "virtual-model",
            "description": "Virtual",
            "routing_strategy": "weighted",
            "providers": [{
                "provider": "vertex",
                "model_id": "gemini-2.5-pro",
                "weight": 0.75,
                "fallback_order": 2,
            }],
            "input_cost_per_1k": 0.002,
            "output_cost_per_1k": 0.01,
            "supports_tools": True,
        })

        response = await client.post("/api/models/push")

        assert response.status_code == 200, response.text
        assert response.json()["pushed"] == 1
        payload = outbound[-1]["json"]["models"][0]
        assert payload["routing_strategy"] == "weighted"
        assert payload["providers"][0]["provider"] == "vertex_ai"
        assert payload["providers"][0]["model_id"] == "gemini-2.5-pro"
        assert payload["providers"][0]["pricing"] == {
            "prompt_token_cost": 0.000002,
            "completion_token_cost": 0.00001,
        }

    async def test_unsupported_strategy_is_rejected_at_write(self, client):
        response = await client.post("/api/models", json={
            "name": "fake",
            "routing_strategy": "ensemble",
        })
        assert response.status_code == 422

    async def test_push_rejects_provider_axon_cannot_route(self, client, outbound):
        await _make_gateway(client)
        await client.post("/api/models", json={
            "name": "invalid-provider",
            "providers": [{"provider": "unknown", "model_id": "some-model"}],
        })

        response = await client.post("/api/models/push")

        assert response.status_code == 422
        assert "unsupported providers unknown" in response.text
        assert outbound == []

    async def test_push_skips_gateway_without_llm_module(
        self, client, monkeypatch
    ):
        await _make_gateway(client)
        await client.post("/api/models", json={
            "name": "virtual-model",
            "providers": [{"provider": "openai", "model_id": "gpt-4o"}],
        })

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, **kwargs):
                return httpx.Response(200, json={"modules_active": []})

            async def post(self, url, json=None, **kwargs):
                raise AssertionError("non-LLM gateway must not receive a registry push")

        from control_plane.routers import model_config

        monkeypatch.setattr(model_config.httpx, "AsyncClient", FakeClient)
        response = await client.post("/api/models/push")

        assert response.status_code == 200
        assert response.json()["pushed"] == 0
        assert response.json()["failed"] == 0
        assert response.json()["skipped"] == 1
