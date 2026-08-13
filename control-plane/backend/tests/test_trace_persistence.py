"""Durable trace storage and production redaction."""

import socket

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.anyio


async def test_production_trace_is_redacted_idempotent_and_restorable(
    client, monkeypatch
):
    from control_plane.database import async_session
    from control_plane.models.database import TraceRecord
    from control_plane.routers import traces

    monkeypatch.setenv("OSTIARI_ENV", "production")
    monkeypatch.setenv("OSTIARI_INGEST_KEY", "i" * 40)
    monkeypatch.setenv(
        "OSTIARI_GATEWAY_CALLBACK_ALLOW",
        "gateway.example.com",
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

    gateway = await client.post(
        "/api/gateways",
        json={
            "id": "trace-gateway",
            "name": "Trace Gateway",
            "endpoint": "https://gateway.example.com",
            "description": "",
        },
    )
    assert gateway.status_code == 200

    event = {
        "trace_id": "trace-persisted",
        "sidecar_id": "trace-gateway",
        "action": "db_query",
        "tier": "allow",
        "params": {
            "sql": "SELECT secret FROM customers",
            "recipient": "private@example.com",
            "input_tokens": 12,
        },
    }
    headers = {"X-Ingest-Key": "i" * 40}
    first = await client.post("/api/traces/ingest", json=event, headers=headers)
    second = await client.post("/api/traces/ingest", json=event, headers=headers)
    assert first.status_code == second.status_code == 200
    assert second.json()["duplicate"] is True

    recent = (await client.get("/api/traces/recent")).json()["traces"]
    stored = next(item for item in recent if item["trace_id"] == "trace-persisted")
    assert stored["params"] == {
        "sql": "[REDACTED]",
        "recipient": "[REDACTED]",
        "input_tokens": 12,
    }

    async with async_session() as db:
        rows = (
            await db.execute(
                select(TraceRecord).where(
                    TraceRecord.trace_id == "trace-persisted"
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert "SELECT secret" not in str(rows[0].event)

    traces._recent_traces.clear()
    async with async_session() as db:
        await traces.load_recent_trace_cache(db)

    restored = traces.recent_traces_for("default")
    assert [item["trace_id"] for item in restored] == ["trace-persisted"]
