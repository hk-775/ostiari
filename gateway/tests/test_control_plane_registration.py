"""Control-plane registration retry and readiness behavior."""

from __future__ import annotations

import threading

import httpx
from ostiari_gateway.lifecycle import LifecycleManager
from ostiari_gateway.models import SidecarConfig
from ostiari_gateway.server import create_app
from ostiari_gateway.trace_reporter import TraceReporter
from starlette.testclient import TestClient


def _registration_error() -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST",
        "http://cp.local/api/gateways/retry-test/register",
    )
    response = httpx.Response(
        422,
        request=request,
        json={"detail": "gateway callback host could not be resolved"},
    )
    return httpx.HTTPStatusError(
        "registration rejected",
        request=request,
        response=response,
    )


def _config() -> SidecarConfig:
    return SidecarConfig(
        sidecar_id="retry-test",
        control_plane_url="http://cp.local",
        callback_url="http://gateway.local:8421",
    )


def test_required_registration_failure_keeps_readiness_false(monkeypatch):
    from ostiari_gateway import server as server_mod

    async def _register(self):
        raise _registration_error()

    monkeypatch.delenv("OSTIARI_ENV", raising=False)
    monkeypatch.setenv("OSTIARI_FAIL_CLOSED_ON_CP_LOSS", "true")
    monkeypatch.setattr(
        server_mod,
        "_CP_REGISTRATION_RETRY_INITIAL_SECONDS",
        60.0,
    )
    monkeypatch.setattr(LifecycleManager, "register", _register)

    app = create_app(_config())
    with TestClient(app) as client:
        readiness = client.get("/ready")
        health = client.get("/health")

        assert readiness.status_code == 503
        assert readiness.json()["control_plane"] == {
            "configured": True,
            "required": True,
            "registered": False,
            "attempts": 1,
            "last_error": "http_422",
            "next_retry_seconds": 60.0,
        }
        assert health.status_code == 200
        assert health.json()["control_plane"]["registered"] is False
        assert health.json()["agent_auth"]["enabled"] is True


def test_registration_retry_restores_readiness_and_agent_auth(monkeypatch):
    from ostiari_gateway import server as server_mod

    calls = 0
    heartbeat_started = threading.Event()

    async def _register(self):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _registration_error()
        return {"config": {}}

    async def _start_heartbeat(self, interval=30):
        assert interval == 30
        heartbeat_started.set()

    async def _start_spend(self, interval_seconds=30.0):
        assert interval_seconds == 30.0

    async def _close(self):
        return None

    monkeypatch.delenv("OSTIARI_ENV", raising=False)
    monkeypatch.setenv("OSTIARI_FAIL_CLOSED_ON_CP_LOSS", "true")
    monkeypatch.setattr(
        server_mod,
        "_CP_REGISTRATION_RETRY_INITIAL_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        server_mod,
        "_CP_REGISTRATION_RETRY_MAX_SECONDS",
        0.01,
    )
    monkeypatch.setattr(LifecycleManager, "register", _register)
    monkeypatch.setattr(
        LifecycleManager,
        "start_heartbeat",
        _start_heartbeat,
    )
    monkeypatch.setattr(
        TraceReporter,
        "start_spend_persistence",
        _start_spend,
    )
    monkeypatch.setattr(TraceReporter, "close", _close)

    app = create_app(_config())
    with TestClient(app) as client:
        assert heartbeat_started.wait(timeout=2.0)

        readiness = client.get("/ready")
        health = client.get("/health")

        assert readiness.status_code == 200
        assert readiness.json()["control_plane"] == {
            "configured": True,
            "required": True,
            "registered": True,
            "attempts": 2,
            "last_error": "",
            "next_retry_seconds": None,
        }
        assert health.json()["agent_auth"]["enabled"] is False

    assert calls == 2
