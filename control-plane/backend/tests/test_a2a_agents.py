"""Tests for the A2A agent persistence router (survives gateway restart)."""

import pytest

pytestmark = pytest.mark.anyio


async def _make_gateway(client, gid="crm-agent"):
    return await client.post("/api/gateways", json={
        "id": gid, "name": gid, "endpoint": "http://localhost:9999", "description": "d",
    })


class TestA2AAgents:
    async def test_register_missing_gateway_404(self, client):
        r = await client.post("/api/a2a-agents/nope", json={"url": "http://localhost:9200"})
        assert r.status_code == 404

    async def test_register_persists_on_gateway_success(self, client, monkeypatch):
        await _make_gateway(client)

        # Stub the gateway call: pretend the gateway connected the agent.
        class _Resp:
            status_code = 200
            def json(self):
                return {"name": "DevOps Assistant", "agent_key": "devops_assistant",
                        "skills": ["deploy", "rollback"], "tools": ["a2a.devops_assistant"]}

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return _Resp()

        monkeypatch.setattr("control_plane.routers.a2a_agents.httpx.AsyncClient", _Client)

        r = await client.post("/api/a2a-agents/crm-agent", json={"url": "http://localhost:9200"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_key"] == "devops_assistant"
        assert body["skills"] == ["deploy", "rollback"]

        # Persisted and listable.
        rows = (await client.get("/api/a2a-agents")).json()
        assert len(rows) == 1
        assert rows[0]["agent_key"] == "devops_assistant"
        assert rows[0]["gateway_id"] == "crm-agent"

    async def test_register_rejected_when_gateway_refuses(self, client, monkeypatch):
        await _make_gateway(client)

        class _Resp:
            status_code = 502
            text = "discovery failed"

        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return _Resp()

        monkeypatch.setattr("control_plane.routers.a2a_agents.httpx.AsyncClient", _Client)

        r = await client.post("/api/a2a-agents/crm-agent", json={"url": "http://bad"})
        assert r.status_code == 502
        # Nothing persisted on failure.
        assert (await client.get("/api/a2a-agents")).json() == []

    async def test_build_config_for_bundle(self, client, monkeypatch, app_and_db):
        await _make_gateway(client)
        from control_plane.database import async_session
        from control_plane.models.database import A2AAgentRecord
        async with async_session() as db:
            db.add(A2AAgentRecord(name="DevOps", agent_key="devops_assistant",
                                  url="http://localhost:9200", gateway_id="crm-agent"))
            await db.commit()
        from control_plane.routers.a2a_agents import build_a2a_config
        async with async_session() as db:
            cfg = await build_a2a_config(db, "crm-agent")
        assert len(cfg) == 1
        assert cfg[0]["url"] == "http://localhost:9200"

    async def test_delete(self, client, monkeypatch, app_and_db):
        await _make_gateway(client)
        from control_plane.database import async_session
        from control_plane.models.database import A2AAgentRecord
        async with async_session() as db:
            rec = A2AAgentRecord(name="X", agent_key="x", url="http://h", gateway_id="crm-agent")
            db.add(rec)
            await db.commit()
            await db.refresh(rec)
            rid = rec.id

        # Stub the best-effort gateway disconnect.
        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def delete(self, *a, **k):
                raise RuntimeError("gateway down")  # delete must still remove the record

        monkeypatch.setattr("control_plane.routers.a2a_agents.httpx.AsyncClient", _Client)

        r = await client.request("DELETE", f"/api/a2a-agents/{rid}")
        assert r.status_code == 200
        assert (await client.get("/api/a2a-agents")).json() == []
