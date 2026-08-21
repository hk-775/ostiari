"""Cross-replica cache and live-trace coordination contracts."""

from __future__ import annotations

import json

import pytest
from control_plane import persistence, redis_client
from control_plane.routers import gateways, traces
from fastapi import HTTPException

pytestmark = pytest.mark.anyio


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def publish(self, channel: str, payload: str) -> int:
        self.published.append((channel, payload))
        return 1


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, event: dict) -> None:
        self.sent.append(event)


class _HealthyRedis:
    async def ping(self) -> bool:
        return True


class _LeaseRedis(_FakeRedis):
    async def eval(
        self,
        _script: str,
        _keys: int,
        key: str,
        token: str,
    ) -> int:
        if self.values.get(key) != token:
            return 0
        del self.values[key]
        return 1


async def test_health_sweep_lease_is_single_owner(monkeypatch):
    redis = _LeaseRedis()

    async def _redis():
        return redis

    monkeypatch.setattr(gateways, "get_redis", _redis)

    first_redis, first_token = await gateways._acquire_health_sweep_lease()
    second_redis, second_token = await gateways._acquire_health_sweep_lease()

    assert first_redis is redis
    assert first_token
    assert second_redis is redis
    assert second_token is None

    await gateways._release_health_sweep_lease(redis, "not-the-owner")
    assert redis.values[gateways._HEALTH_SWEEP_LEASE_KEY] == first_token

    await gateways._release_health_sweep_lease(redis, first_token)
    assert gateways._HEALTH_SWEEP_LEASE_KEY not in redis.values


async def test_session_parent_is_atomic_across_replicas(monkeypatch):
    redis = _FakeRedis()

    async def _redis():
        return redis

    monkeypatch.setattr(traces, "get_redis", _redis)
    first = {"trace_id": "trace-a", "session_id": "session-1"}
    second = {"trace_id": "trace-b", "session_id": "session-1"}

    await traces._assign_parent_distributed(first, "org-a")
    await traces._assign_parent_distributed(second, "org-a")

    assert first["parent_trace_id"] == "trace-a"
    assert second["parent_trace_id"] == "trace-a"
    assert first["is_span_root"] is True
    assert second["is_span_root"] is False


async def test_trace_publish_reaches_local_and_remote_replica(monkeypatch):
    redis = _FakeRedis()
    local = _FakeWebSocket()
    remote = _FakeWebSocket()
    traces._ws_clients["org-a"].add(local)  # type: ignore[arg-type]

    async def _redis():
        return redis

    monkeypatch.setattr(traces, "get_redis", _redis)
    event = {"trace_id": "trace-a", "action": "tool"}

    assert await traces._publish_trace("org-a", event) == 1
    assert local.sent == [event]
    assert len(redis.published) == 1

    traces._ws_clients["org-a"].clear()
    traces._ws_clients["org-a"].add(remote)  # type: ignore[arg-type]
    payload = json.loads(redis.published[0][1])
    payload["source"] = "other-replica"
    await traces._handle_trace_message(json.dumps(payload))

    assert remote.sent == [event]
    assert [item["trace_id"] for item in traces._recent_traces["org-a"]] == [
        "trace-a"
    ]


async def test_successful_publish_does_not_mask_subscriber_failure(monkeypatch):
    redis = _FakeRedis()

    async def _redis():
        return redis

    monkeypatch.setattr(traces, "get_redis", _redis)
    traces._trace_bus_errors["subscribe"] = "connection lost"

    await traces._publish_trace("org-a", {"trace_id": "trace-a"})

    assert traces._trace_bus_errors["publish"] == ""
    assert traces.trace_bus_error() == "subscribe: connection lost"


async def test_scaled_trace_parent_assignment_fails_closed_without_redis(
    monkeypatch,
):
    async def _redis():
        return None

    monkeypatch.setattr(traces, "get_redis", _redis)
    monkeypatch.setenv("OSTIARI_CONTROL_PLANE_REPLICAS", "2")
    for operation in traces._trace_bus_errors:
        traces._trace_bus_errors[operation] = ""

    with pytest.raises(HTTPException) as exc:
        await traces._assign_parent_distributed(
            {"trace_id": "trace-a", "session_id": "session-1"},
            "org-a",
        )

    assert exc.value.status_code == 503
    assert traces.trace_bus_error() == "parent: Redis unavailable"


async def test_scaled_readiness_requires_healthy_coordination(
    client,
    monkeypatch,
):
    async def _redis():
        return _HealthyRedis()

    monkeypatch.setenv("OSTIARI_CONTROL_PLANE_REPLICAS", "2")
    monkeypatch.setattr(redis_client, "get_redis", _redis)

    response = await client.get("/api/ready")

    assert response.status_code == 200
    assert response.json()["redis"] == "available"


async def test_scaled_readiness_exposes_sync_failure(client, monkeypatch):
    async def _redis():
        return _HealthyRedis()

    monkeypatch.setenv("OSTIARI_CONTROL_PLANE_REPLICAS", "2")
    monkeypatch.setattr(redis_client, "get_redis", _redis)
    persistence._runtime_sync_error = "revision poll failed"

    response = await client.get("/api/ready")

    assert response.status_code == 503
    assert "runtime synchronization failed" in response.json()["detail"]
