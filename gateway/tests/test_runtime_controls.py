"""Regression tests for live budget, classification, and registry controls."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from ostiari_gateway.budget_reset import (
    BudgetResetScheduler,
    latest_reset_boundary,
    next_reset_at,
)
from ostiari_gateway.models import SidecarConfig
from ostiari_gateway.modules.llm_gateway.axon_router import AxonRouter
from ostiari_gateway.server import create_app
from starlette.testclient import TestClient


def test_reset_boundaries_are_utc_period_boundaries():
    now = datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc)
    assert latest_reset_boundary("daily", now) == datetime(
        2026, 8, 11, tzinfo=timezone.utc
    )
    assert next_reset_at("daily", now) == datetime(
        2026, 8, 12, tzinfo=timezone.utc
    )
    assert next_reset_at("weekly", now).weekday() == 0
    assert next_reset_at("monthly", now).day == 1


@pytest.mark.anyio
async def test_scheduler_catches_up_after_missed_boundary():
    now = datetime(2026, 8, 11, 12, 30, tzinfo=timezone.utc)
    resets: list[datetime] = []
    scheduler = BudgetResetScheduler(resets.append, now=lambda: now)
    scheduler.configure({
        "schedule": "daily",
        "configured_at": "2026-08-10T10:00:00+00:00",
    })
    await asyncio.sleep(0)
    assert resets == [now]
    assert scheduler.last_reset_at == now
    await scheduler.close()


def test_manual_reset_clears_gateway_and_agent_spend():
    with TestClient(create_app(initial_config=SidecarConfig(sidecar_id="reset-test"))) as client:
        quota = client.app.state.quota_enforcer
        quota.configure({"budget_limit_usd": 10})
        quota.record_spend(2.5)

        auth = client.app.state.agent_auth
        auth.configure({
            "quota_enabled": True,
            "agents": {
                "agent-1": {
                    "allowed_models": ["*"],
                    "allowed_providers": ["*"],
                    "budget_usd": 10,
                },
            },
        })
        auth.record_agent_spend("agent-1", 3.5)

        response = client.post("/config/quota/reset-spend")
        assert response.status_code == 200
        assert quota.get_status()["current_spend"] == 0
        assert auth.list_agents()[0]["spend_usd"] == 0


def test_full_config_preserves_omitted_broker_state_and_clears_explicit_empty():
    with TestClient(create_app(initial_config=SidecarConfig(sidecar_id="broker-test"))) as client:
        policy = client.app.state.broker_policy
        policy.configure([{"provider": "openai", "status": "depleted"}])

        response = client.post("/config", json={"tools": []})
        assert response.status_code == 200
        assert policy.blocked_providers == {"openai"}

        response = client.post(
            "/config",
            json={"tools": [], "broker_pools": []},
        )
        assert response.status_code == 200
        assert policy.blocked_providers == set()


class _Registry:
    def __init__(self) -> None:
        self.models = {"old": object()}

    def validate(self, config):
        return []

    def _parse_entry(self, entry):
        return dict(entry)


def test_axon_registry_is_replaced_at_runtime():
    registry = _Registry()
    axon = AxonRouter()
    axon._built = True
    axon._available = True
    axon._agent = SimpleNamespace(
        router=SimpleNamespace(model_registry=registry, available_providers=None)
    )
    catalog = {
        "models": [{
            "name": "virtual-model",
            "description": "runtime",
            "routing_strategy": "weighted",
            "providers": [{
                "provider": "openai",
                "model_id": "gpt-4o",
                "weight": 1,
                "fallback_order": 0,
            }],
        }],
    }

    result = axon.configure_model_registry(catalog)

    assert result == {"status": "applied", "models": 1}
    assert set(registry.models) == {"virtual-model"}
    assert axon.model_registry_config() == catalog


def test_invalid_axon_registry_does_not_replace_last_applied_catalog():
    class _InvalidRegistry(_Registry):
        def validate(self, config):
            if config["models"][0]["name"] == "invalid":
                return [SimpleNamespace(field="models[0]", message="invalid")]
            return []

    registry = _InvalidRegistry()
    axon = AxonRouter()
    axon._built = True
    axon._available = True
    axon._agent = SimpleNamespace(
        router=SimpleNamespace(model_registry=registry, available_providers=None)
    )
    valid = {
        "models": [{
            "name": "valid",
            "description": "runtime",
            "providers": [{"provider": "openai", "model_id": "gpt-4o"}],
        }],
    }
    axon.configure_model_registry(valid)

    with pytest.raises(ValueError, match="invalid model registry"):
        axon.configure_model_registry({
            "models": [{
                "name": "invalid",
                "description": "runtime",
                "providers": [{"provider": "openai", "model_id": "gpt-4o"}],
            }],
        })

    assert axon.model_registry_config() == valid
    assert set(registry.models) == {"valid"}
