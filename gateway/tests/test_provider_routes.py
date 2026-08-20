"""Gateway provider-route hot reload and redaction tests."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from ostiari_gateway.models import ModulesConfig, SidecarConfig
from ostiari_gateway.modules.llm_gateway.axon_router import AxonRouter
from ostiari_gateway.server import create_app
from starlette.testclient import TestClient


class _Factory:
    def __init__(self) -> None:
        self.routes: list[dict] = []
        self.available_providers = frozenset()

    def configure_routes(self, routes: list[dict]) -> dict[str, int]:
        if any(not route.get("route_id") for route in routes):
            raise ValueError("route_id is required")
        self.routes = deepcopy(routes)
        self.available_providers = frozenset(
            route["provider"]
            for route in routes
            if route.get("enabled", True)
        )
        return {
            "routes": len(routes),
            "providers": len(self.available_providers),
        }

    def route_snapshot(self) -> list[dict]:
        return [
            {
                key: deepcopy(value)
                for key, value in route.items()
                if key not in {"credentials", "extra_headers", "extra_params"}
            }
            | {
                "has_credentials": bool(route.get("credentials")),
                "status": "healthy",
                "inflight": 0,
            }
            for route in self.routes
        ]


class _EmbeddedRouter:
    def __init__(self, factory: _Factory, core: SimpleNamespace) -> None:
        self._factory = factory
        self._runtime = SimpleNamespace(
            router=core,
            provider_factory=factory,
        )

    def configure_routes(self, routes: list[dict]) -> dict[str, int]:
        result = self._factory.configure_routes(routes)
        self._runtime.router.available_providers = (
            self._factory.available_providers
        )
        return result

    def route_snapshot(self) -> list[dict]:
        return self._factory.route_snapshot()

    async def close(self) -> None:
        return None


def _route(
    route_id: str = "openai:primary",
    *,
    api_key: str = "route-secret",
) -> dict:
    return {
        "route_id": route_id,
        "provider": "openai",
        "endpoint": "https://api.openai.example",
        "auth_type": "api_key",
        "credentials": {"api_key": api_key},
        "extra_headers": {"X-Private": "header-secret"},
        "extra_params": {"proxy_url": "https://proxy-secret@example.test"},
        "weight": 1,
        "priority": 0,
        "enabled": True,
    }


def _ready_axon() -> tuple[AxonRouter, _Factory, SimpleNamespace]:
    factory = _Factory()
    router = SimpleNamespace(
        available_providers=None,
        model_registry=SimpleNamespace(models={}),
    )
    axon = AxonRouter()
    axon._built = True
    axon._available = True
    axon._router = _EmbeddedRouter(factory, router)
    return axon, factory, router


def test_axon_route_catalog_replaces_runtime_providers_atomically():
    axon, factory, router = _ready_axon()

    result = axon.configure_provider_routes([_route()])

    assert result == {"status": "applied", "routes": 1, "providers": 1}
    assert factory.routes[0]["credentials"] == {
        "api_key": "route-secret"
    }
    assert router.available_providers == frozenset({"openai"})
    snapshot = axon.provider_route_snapshot()
    assert snapshot[0]["has_credentials"] is True
    assert "credentials" not in snapshot[0]
    assert "route-secret" not in repr(snapshot)

    cleared = axon.configure_provider_routes([])
    assert cleared == {"status": "applied", "routes": 0, "providers": 0}
    assert router.available_providers == frozenset()


def test_invalid_route_catalog_preserves_last_applied_config():
    axon, factory, _router = _ready_axon()
    axon.configure_provider_routes([_route()])

    with pytest.raises(ValueError, match="route_id"):
        axon.configure_provider_routes([{"provider": "openai"}])

    assert factory.routes == [_route()]
    assert axon._provider_routes_config == [_route()]


def test_pending_route_snapshot_never_exposes_secrets():
    axon = AxonRouter()
    axon._built = True
    axon._available = False
    axon._error = "not installed"

    route = _route()
    route["api_key"] = "top-level-secret"
    route["unexpected_private_value"] = "must-not-escape"
    result = axon.configure_provider_routes([route])
    snapshot = axon.provider_route_snapshot()

    assert result["status"] == "pending"
    assert snapshot[0]["status"] == "pending"
    assert snapshot[0]["has_credentials"] is True
    assert "credentials" not in snapshot[0]
    assert "route-secret" not in repr(snapshot)
    assert "top-level-secret" not in repr(snapshot)
    assert "must-not-escape" not in repr(snapshot)


def test_route_endpoint_rejects_embedded_userinfo():
    axon = AxonRouter()
    route = _route()
    route["endpoint"] = "https://user:secret@api.example"

    with pytest.raises(ValueError, match="userinfo"):
        axon.configure_provider_routes([route])


def test_config_endpoints_apply_clear_and_redact_routes(monkeypatch):
    factories: list[_Factory] = []

    def fake_ensure(self: AxonRouter) -> None:
        if self._built:
            return
        factory = _Factory()
        factories.append(factory)
        self._built = True
        self._available = True
        self._error = ""
        self._root = "/fake/axon"
        core = SimpleNamespace(
            available_providers=None,
            model_registry=SimpleNamespace(models={}),
        )
        self._router = _EmbeddedRouter(factory, core)

    monkeypatch.setattr(AxonRouter, "_ensure", fake_ensure)
    monkeypatch.delenv("OSTIARI_CONFIG_ADMIN_KEY", raising=False)
    initial = SidecarConfig(
        sidecar_id="route-test",
        modules=ModulesConfig(llm_gateway=True),
        provider_routes=[_route()],
    )

    with TestClient(create_app(initial_config=initial)) as client:
        assert factories[0].routes == [_route()]

        runtime = client.get("/config/provider-routes")
        assert runtime.status_code == 200
        assert runtime.json()["routes"][0]["status"] == "healthy"
        assert "credentials" not in runtime.json()["routes"][0]

        full = client.get("/config")
        assert full.status_code == 200
        stored = full.json()["provider_routes"][0]
        assert stored["credentials"]["api_key"] == "***REDACTED***"
        assert stored["extra_headers"]["X-Private"] == "***REDACTED***"
        assert stored["extra_params"]["proxy_url"] == "***REDACTED***"
        assert "route-secret" not in full.text

        replacement = _route("openai:backup", api_key="backup-secret")
        applied = client.post(
            "/config/provider-routes",
            json={"routes": [replacement]},
        )
        assert applied.status_code == 200, applied.text
        assert factories[0].routes == [replacement]

        cleared = client.post(
            "/config",
            json={"tools": [], "provider_routes": []},
        )
        assert cleared.status_code == 200, cleared.text
        assert factories[0].routes == []
