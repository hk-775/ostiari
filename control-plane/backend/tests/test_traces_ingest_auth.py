"""Tests for shared-secret auth on the trace ingest endpoint.

Machine callers (gateways/sidecars, e.g. AxonLLM) push to /api/traces/ingest.
When OSTIARI_INGEST_KEY is set, they must present a matching X-Ingest-Key.
Unset → fail-open (dev/demo).
"""

import pytest

pytestmark = pytest.mark.anyio

_EVENT = {
    "gateway_id": "axonllm",
    "action": "chat.completion",
    "tier": "allow",
    "score": 0,
    "model": "claude-sonnet",
}


class TestIngestAuth:
    async def test_open_when_key_unset(self, client, monkeypatch):
        monkeypatch.delenv("OSTIARI_INGEST_KEY", raising=False)
        r = await client.post("/api/traces/ingest", json=_EVENT)
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_rejected_without_header_when_key_set(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_INGEST_KEY", "s3cret")
        r = await client.post("/api/traces/ingest", json=_EVENT)
        assert r.status_code == 401

    async def test_rejected_with_wrong_key(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_INGEST_KEY", "s3cret")
        r = await client.post(
            "/api/traces/ingest", json=_EVENT, headers={"X-Ingest-Key": "wrong"}
        )
        assert r.status_code == 401

    async def test_accepted_with_correct_key(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_INGEST_KEY", "s3cret")
        r = await client.post(
            "/api/traces/ingest", json=_EVENT, headers={"X-Ingest-Key": "s3cret"}
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    async def test_recent_reflects_only_authed_ingest(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_INGEST_KEY", "s3cret")
        # rejected push does not land
        await client.post("/api/traces/ingest", json=_EVENT)
        # authed push does
        await client.post(
            "/api/traces/ingest", json={**_EVENT, "action": "authed"},
            headers={"X-Ingest-Key": "s3cret"},
        )
        recent = (await client.get("/api/traces/recent")).json()["traces"]
        actions = [t.get("action") for t in recent]
        assert "authed" in actions
        assert actions.count("chat.completion") == 0
