"""Tests for the human-in-the-loop approval queue."""

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _clear():
    from control_plane.routers import approvals
    approvals._pending.clear()
    yield
    approvals._pending.clear()


async def _create(client, action="db_delete", score=60):
    return await client.post("/api/approvals", json={
        "agent_id": "ops-agent", "gateway_id": "ops-agent",
        "action": action, "params": {"sql": "DELETE FROM users WHERE 1=1"},
        "score": score, "reason": "intervene tier",
    })


class TestApprovals:
    async def test_create_is_pending(self, client):
        r = await _create(client)
        assert r.status_code == 200
        a = r.json()
        assert a["status"] == "pending" and a["id"].startswith("apr-")

    async def test_pending_queue_lists_only_pending(self, client):
        await _create(client)
        await _create(client, action="send_email")
        q = (await client.get("/api/approvals")).json()
        assert len(q) == 2 and all(a["status"] == "pending" for a in q)

    async def test_approve_records_who_and_when(self, client):
        aid = (await _create(client)).json()["id"]
        r = await client.post(f"/api/approvals/{aid}/decision",
                              json={"decision": "approve", "decided_by": "alice"})
        a = r.json()
        assert a["status"] == "approved"
        assert a["decided_by"] == "alice" and a["decided_at"]

    async def test_deny(self, client):
        aid = (await _create(client)).json()["id"]
        r = await client.post(f"/api/approvals/{aid}/decision", json={"decision": "deny"})
        assert r.json()["status"] == "denied"

    async def test_decided_leaves_pending_queue(self, client):
        aid = (await _create(client)).json()["id"]
        await client.post(f"/api/approvals/{aid}/decision", json={"decision": "approve"})
        assert (await client.get("/api/approvals")).json() == []       # pending empty
        assert len((await client.get("/api/approvals/all")).json()) == 1  # audit keeps it

    async def test_double_decision_409(self, client):
        aid = (await _create(client)).json()["id"]
        await client.post(f"/api/approvals/{aid}/decision", json={"decision": "approve"})
        r = await client.post(f"/api/approvals/{aid}/decision", json={"decision": "deny"})
        assert r.status_code == 409

    async def test_bad_decision_400(self, client):
        aid = (await _create(client)).json()["id"]
        r = await client.post(f"/api/approvals/{aid}/decision", json={"decision": "maybe"})
        assert r.status_code == 400

    async def test_decide_missing_404(self, client):
        r = await client.post("/api/approvals/nope/decision", json={"decision": "approve"})
        assert r.status_code == 404

    async def test_status_filter(self, client):
        aid = (await _create(client)).json()["id"]
        await _create(client, action="x")
        await client.post(f"/api/approvals/{aid}/decision", json={"decision": "approve"})
        approved = (await client.get("/api/approvals?status=approved")).json()
        assert len(approved) == 1 and approved[0]["status"] == "approved"
