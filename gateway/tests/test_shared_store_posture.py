"""Fail-closed Redis behavior and readiness reporting."""

from __future__ import annotations

import pytest
from ostiari_gateway import shared_store
from ostiari_gateway.models import SidecarConfig
from ostiari_gateway.server import create_app
from starlette.testclient import TestClient


class _BrokenRedis:
    def register_script(self, script):
        def run(*args, **kwargs):
            raise ConnectionError("redis unavailable")

        return run

    def ping(self):
        raise ConnectionError("redis unavailable")

    def get(self, key):
        raise ConnectionError("redis unavailable")

    def incrbyfloat(self, key, value):
        raise ConnectionError("redis unavailable")

    def delete(self, key):
        raise ConnectionError("redis unavailable")

    def hset(self, key, mapping):
        raise ConnectionError("redis unavailable")

    def hgetall(self, key):
        raise ConnectionError("redis unavailable")

    def xadd(self, key, fields):
        raise ConnectionError("redis unavailable")

    def xrange(self, key, min="-", max="+", count=100):
        raise ConnectionError("redis unavailable")

    def xdel(self, key, *receipts):
        raise ConnectionError("redis unavailable")

    def xlen(self, key):
        raise ConnectionError("redis unavailable")


class TestRequiredStore:
    def test_required_store_fails_closed_on_runtime_errors(self):
        store = shared_store.SharedStore(_BrokenRedis(), required=True)

        assert store.rate_allow("agent", 10) is False
        assert store.budget_reserve("budget", 0.1, 1.0) is False
        assert store.budget_spend("budget") is None
        assert store.wallet_debit("agent", 0.1)[0] is False
        status = store.status(check=True)
        assert status["required"] is True
        assert status["healthy"] is False
        assert "redis unavailable" in status["last_error"]

    def test_optional_store_preserves_dev_fallback(self):
        store = shared_store.SharedStore(_BrokenRedis(), required=False)

        assert store.rate_allow("agent", 10) is True
        assert store.budget_reserve("budget", 0.1, 1.0) is True
        assert store.budget_spend("budget") == 0.0

    def test_required_configuration_without_endpoint_refuses_start(
        self, monkeypatch
    ):
        shared_store.reset_shared_store()
        monkeypatch.setenv("OSTIARI_REQUIRE_REDIS", "true")
        monkeypatch.delenv("OSTIARI_REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_ENDPOINT", raising=False)
        try:
            with pytest.raises(RuntimeError, match="Redis is required"):
                shared_store.get_shared_store()
        finally:
            shared_store.reset_shared_store()

    def test_redis_password_is_redacted(self):
        safe = shared_store._safe_redis_url(
            "redis://default:super-secret@redis.internal:6379/0"
        )
        assert "super-secret" not in safe
        assert safe == "redis://***@redis.internal:6379/0"


class _DegradedStore:
    required = True

    def attach(self):
        return None

    def status(self, *, check=False):
        return {
            "configured": True,
            "required": True,
            "healthy": False,
            "last_error": "ping: unavailable",
        }

    def budget_spend(self, key):
        return None

    def outbox_enqueue(self, stream, event_id, payload):
        return False

    def outbox_read(self, stream, *, count=100):
        return None

    def outbox_ack(self, stream, receipts):
        return False

    def outbox_depth(self, stream):
        return None


class _HealthyCacheBrokenOutbox(_DegradedStore):
    def status(self, *, check=False):
        return {
            "configured": True,
            "required": True,
            "healthy": True,
            "last_error": "",
        }


def test_readiness_fails_when_required_redis_is_unhealthy(monkeypatch):
    degraded = _DegradedStore()
    monkeypatch.setattr(shared_store, "get_shared_store", lambda: degraded)
    monkeypatch.setattr(shared_store, "shared_store_required", lambda: True)

    client = TestClient(create_app(initial_config=SidecarConfig(sidecar_id="ready")))
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["redis"]["healthy"] is False


def test_readiness_fails_when_required_outbox_is_unhealthy(monkeypatch):
    degraded = _HealthyCacheBrokenOutbox()
    monkeypatch.setattr(shared_store, "get_shared_store", lambda: degraded)
    monkeypatch.setattr(shared_store, "shared_store_required", lambda: True)

    client = TestClient(create_app(initial_config=SidecarConfig(sidecar_id="ready")))
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["redis"]["healthy"] is True
    assert response.json()["delivery"]["traces"]["healthy"] is False
