"""Crash-safe event delivery over the shared Redis store."""

from __future__ import annotations

from collections import defaultdict

import httpx
import pytest
from ostiari_gateway.event_outbox import EventOutbox, scoped_stream
from ostiari_gateway.modules.llm_gateway.cost_reporter import CostReporter
from ostiari_gateway.shared_store import SharedStore
from ostiari_gateway.trace_reporter import TraceReporter


class _Redis:
    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
        self.sequence = 0
        self.fail_enqueues = 0
        self.fail_deletes = 0

    def register_script(self, script):
        return lambda *args, **kwargs: 1

    def ping(self):
        return True

    def xadd(self, key, fields):
        if self.fail_enqueues:
            self.fail_enqueues -= 1
            raise ConnectionError("stream write failed")
        self.sequence += 1
        receipt = f"{self.sequence}-0"
        self.streams[key].append((receipt, dict(fields)))
        return receipt

    def xrange(self, key, min="-", max="+", count=100):
        return list(self.streams[key][:count])

    def xdel(self, key, *receipts):
        if self.fail_deletes:
            self.fail_deletes -= 1
            raise ConnectionError("stream acknowledgement failed")
        before = len(self.streams[key])
        wanted = set(receipts)
        self.streams[key] = [
            row for row in self.streams[key] if row[0] not in wanted
        ]
        return before - len(self.streams[key])

    def xlen(self, key):
        return len(self.streams[key])


def _store(redis: _Redis | None = None) -> tuple[SharedStore, _Redis]:
    client = redis or _Redis()
    return SharedStore(client, prefix="outbox-test", required=True), client


class _Client:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.calls: list[dict] = []

    async def post(self, url, *, json, headers):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return httpx.Response(
            self.statuses.pop(0),
            request=httpx.Request("POST", url),
        )

    async def aclose(self):
        return None


def test_scoped_stream_is_stable_and_isolates_gateway_identity():
    assert scoped_stream("traces", "gateway-a") == scoped_stream(
        "traces", "gateway-a"
    )
    assert scoped_stream("traces", "gateway-a") != scoped_stream(
        "traces", "gateway-b"
    )
    assert "gateway-a" not in scoped_stream("traces", "gateway-a")


def test_outbox_survives_restart_and_acknowledges_only_confirmed_event():
    store, _ = _store()
    stream = scoped_stream("traces", "gateway-a")
    first = EventOutbox(stream, id_field="event_id")
    first.attach_store(store)
    assert first.enqueue({"event_id": "evt-1", "value": 7}) is True

    restarted = EventOutbox(stream, id_field="event_id")
    restarted.attach_store(store)
    pending = restarted.pending()
    assert [(event.event_id, event.payload["value"]) for event in pending] == [
        ("evt-1", 7)
    ]
    assert restarted.acknowledge(pending) is True
    assert restarted.pending() == []


def test_failed_acknowledgement_retains_event_for_idempotent_retry():
    store, redis = _store()
    outbox = EventOutbox(scoped_stream("payments", "gateway-a"), id_field="event_id")
    outbox.attach_store(store)
    outbox.enqueue({"event_id": "charge-1"})
    pending = outbox.pending()

    redis.fail_deletes = 1
    assert outbox.acknowledge(pending) is False
    assert outbox.status()["healthy"] is False
    retried = outbox.pending()
    assert [event.event_id for event in retried] == ["charge-1"]
    assert outbox.acknowledge(retried) is True


def test_memory_event_migrates_after_redis_recovers():
    store, redis = _store()
    redis.fail_enqueues = 1
    outbox = EventOutbox(scoped_stream("costs", "gateway-a"), id_field="event_id")
    outbox.attach_store(store)

    assert outbox.enqueue({"event_id": "cost-1"}) is False
    pending = outbox.pending()
    assert [event.event_id for event in pending] == ["cost-1"]
    assert pending[0].durable is True
    assert outbox.status() == {
        "configured": True,
        "required": True,
        "healthy": True,
        "last_error": "",
        "pending": 1,
    }


def test_identity_cannot_change_while_events_are_pending():
    outbox = EventOutbox(
        scoped_stream("traces", "gateway-a"),
        id_field="event_id",
    )
    outbox.enqueue({"event_id": "evt-1"})
    with pytest.raises(RuntimeError, match="pending events"):
        outbox.rebind(scoped_stream("traces", "gateway-b"))


def test_identity_cannot_change_when_durable_depth_is_unknown():
    store, redis = _store()
    outbox = EventOutbox(
        scoped_stream("traces", "gateway-a"),
        id_field="event_id",
    )
    outbox.attach_store(store)
    redis.xlen = lambda key: (_ for _ in ()).throw(ConnectionError("unavailable"))

    with pytest.raises(RuntimeError, match="cannot verify empty outbox"):
        outbox.rebind(scoped_stream("traces", "gateway-b"))


@pytest.mark.anyio
async def test_cost_reporter_recovers_same_event_after_process_restart():
    store, _ = _store()
    first = CostReporter("http://cp.local", "gateway-a", shared_store=store)
    failed = _Client([503])
    first._client = failed  # type: ignore[assignment]
    await first.report(
        model="model",
        input_tokens=2,
        output_tokens=1,
        total_tokens=3,
    )
    await first.flush()

    restarted = CostReporter("http://cp.local", "gateway-a", shared_store=store)
    delivered = _Client([200])
    restarted._client = delivered  # type: ignore[assignment]
    await restarted.flush()

    assert failed.calls[0]["json"] == delivered.calls[0]["json"]
    assert delivered.calls[0]["json"][0]["event_id"]
    assert restarted.delivery_status()["costs"]["pending"] == 0


@pytest.mark.anyio
async def test_trace_reporter_recovers_same_event_after_process_restart():
    store, _ = _store()
    first = TraceReporter("http://cp.local", "gateway-a")
    first.attach_shared_store(store)
    failed = _Client([503])
    first._client = failed  # type: ignore[assignment]
    await first.report(action="tool", tier="allow", score=0, duration_ms=1)

    restarted = TraceReporter("http://cp.local", "gateway-a")
    restarted.attach_shared_store(store)
    delivered = _Client([200])
    restarted._client = delivered  # type: ignore[assignment]
    await restarted.flush_traces()

    assert failed.calls[0]["json"] == delivered.calls[0]["json"]
    assert delivered.calls[0]["json"]["trace_id"]
    assert restarted.delivery_status()["traces"]["pending"] == 0
