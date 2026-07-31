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


class TestApprovalDemoSeed:
    """The seeded queue exists so the Approvals page isn't empty in the demo.

    Its value is entirely in being *consistent* with the rest of the demo: a
    score the real scorer couldn't produce, or a pending call that no trace
    shows being paused, turns the page into a contradiction an operator will
    notice before a bug report does.
    """

    def test_seeds_pending_and_decided(self):
        from control_plane.demo_seed import (
            _DECIDED_APPROVALS,
            _PENDING_APPROVALS,
            seed_demo_approvals,
        )
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.approvals import _pending

        seed_demo_approvals()
        q = _pending[DEFAULT_ORG].values()
        assert len([a for a in q if a.status == "pending"]) == len(_PENDING_APPROVALS)
        assert len([a for a in q if a.status != "pending"]) == len(_DECIDED_APPROVALS)

    def test_seeding_twice_does_not_duplicate(self):
        from control_plane.demo_seed import seed_demo_approvals
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.approvals import _pending

        seed_demo_approvals()
        first = len(_pending[DEFAULT_ORG])
        seed_demo_approvals()
        assert len(_pending[DEFAULT_ORG]) == first

    async def test_a_real_pending_approval_suppresses_the_seed(self, client):
        """A live gateway's queue must never be padded with demo rows — an
        operator would be deciding fictional calls alongside real ones."""
        from control_plane.demo_seed import seed_demo_approvals
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.approvals import _pending

        await _create(client)
        seed_demo_approvals()
        assert len(_pending[DEFAULT_ORG]) == 1

    def test_every_score_is_the_sum_of_its_signals(self):
        """Scores aren't hand-picked numbers; they're what the signals add to."""
        from control_plane.demo_seed import _approval_score, seed_demo_approvals
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.approvals import _pending

        seed_demo_approvals()
        for a in _pending[DEFAULT_ORG].values():
            assert a.score == _approval_score(a.action), a.action

    def test_every_score_lands_in_the_intervene_band(self):
        """Only the intervene tier reaches this queue: allow runs, block doesn't.
        A seeded 20 or 95 would depict a call that could not be here at all."""
        from control_plane.demo_seed import seed_demo_approvals
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.approvals import _pending

        from ostiari.models import ThresholdConfig

        t = ThresholdConfig()
        seed_demo_approvals()
        for a in _pending[DEFAULT_ORG].values():
            assert t.allow_max < a.score <= t.intervene_max, (a.action, a.score)

    def test_reasons_are_what_explain_would_have_said(self):
        """The gateway stores explain(result).summary, so the seed must too —
        not prose that merely resembles it."""
        from control_plane.demo_seed import _approval_reason, seed_demo_approvals
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.approvals import _pending

        seed_demo_approvals()
        for a in _pending[DEFAULT_ORG].values():
            assert a.reason == _approval_reason(a.action, a.score)
            assert "flagged for human approval" in a.reason

    def test_pending_rows_carry_no_decision_and_decided_rows_do(self):
        from control_plane.demo_seed import seed_demo_approvals
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.approvals import _pending

        seed_demo_approvals()
        for a in _pending[DEFAULT_ORG].values():
            if a.status == "pending":
                assert not a.decided_by and not a.decided_at, a.id
            else:
                # A decision needs an author and a time after the request, or the
                # audit trail it's meant to demonstrate says nothing.
                assert a.decided_by and a.decided_at > a.created_at, a.id

    def test_history_shows_both_outcomes(self):
        """A queue where every answer was 'yes' wouldn't show denial is real."""
        from control_plane.demo_seed import seed_demo_approvals
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.approvals import _pending

        seed_demo_approvals()
        statuses = {a.status for a in _pending[DEFAULT_ORG].values()}
        assert {"pending", "approved", "denied"} <= statuses

    def test_pending_calls_match_the_intervene_traces(self):
        """Live Traces and Approvals describe one fleet. Every pending approval
        must correspond to a seeded intervene trace on agent, action, score and
        params — otherwise clicking from a paused trace to the queue finds a
        different, fictional call."""
        from control_plane.demo_seed import _PENDING_APPROVALS, _approval_score
        from control_plane.models.database import DEFAULT_ORG
        from control_plane.routers.traces import _recent_traces, seed_traces

        seed_traces()
        traces = [t for t in _recent_traces[DEFAULT_ORG] if t["tier"] == "intervene"]
        for agent, _gw, action, params, _age in _PENDING_APPROVALS:
            assert any(
                t["agent_id"] == agent and t["action"] == action
                and t["score"] == _approval_score(action) and t["params"] == params
                for t in traces
            ), f"no intervene trace for pending approval {agent}/{action}"

    async def test_the_seeded_queue_can_actually_be_decided(self, client):
        """The rows are real Approval objects on the real routes, not display
        fixtures — Approve/Deny in the UI must work on them."""
        from control_plane.demo_seed import seed_demo_approvals

        seed_demo_approvals()
        pending = (await client.get("/api/approvals")).json()
        aid = pending[0]["id"]
        r = await client.post(f"/api/approvals/{aid}/decision",
                              json={"decision": "approve", "decided_by": "operator"})
        assert r.status_code == 200 and r.json()["status"] == "approved"
        assert len((await client.get("/api/approvals")).json()) == len(pending) - 1

    def test_seeded_actions_all_have_signals_defined(self):
        """A row whose action has no signal set would KeyError at seed time and
        take the whole control-plane startup down with it."""
        from control_plane.demo_seed import (
            _APPROVAL_SIGNALS,
            _DECIDED_APPROVALS,
            _PENDING_APPROVALS,
        )

        used = {r[2] for r in _PENDING_APPROVALS} | {r[2] for r in _DECIDED_APPROVALS}
        assert used <= set(_APPROVAL_SIGNALS)
        assert set(_APPROVAL_SIGNALS) == used, "unused signal entries drift out of date"


def _hdr(org: str) -> dict[str, str]:
    from control_plane.auth.service import create_access_token
    tok = create_access_token(user_id=1, email=f"{org}@t.io", role="admin", org=org)
    return {"Authorization": f"Bearer {tok}"}


class TestApprovalOrgIsolation:
    """The queue was a flat id-keyed dict, so it was shared across tenants. That
    exposed the agent's raw tool params (the SQL it wanted to run, recipients,
    payloads) plus the reviewer's identity to every other org — and let any org
    approve another's blocked call, which is a governance bypass, not just a leak.

    The gateway posts and polls without a user token, so the owning org has to
    come from its `gateways` row.
    """

    A, B = _hdr("org-a"), _hdr("org-b")

    async def _gateway(self, client, headers, gid):
        await client.post("/api/gateways", headers=headers,
                          json={"id": gid, "name": gid, "endpoint": "http://x:8421", "description": ""})

    async def _pending_for(self, client, gid):
        # Tokenless create — how the gateway actually reports an intervene call.
        r = await client.post("/api/approvals", json={
            "agent_id": "ops-agent", "gateway_id": gid, "action": "db_delete",
            "params": {"sql": f"DELETE FROM {gid}_users"}, "score": 60, "reason": "intervene",
        })
        assert r.status_code == 200
        return r.json()["id"]

    async def _both(self, client):
        await self._gateway(client, self.A, "apr-gw-a")
        await self._gateway(client, self.B, "apr-gw-b")
        return await self._pending_for(client, "apr-gw-a"), await self._pending_for(client, "apr-gw-b")

    async def test_queue_is_scoped_to_the_owning_org(self, client):
        a_id, b_id = await self._both(client)
        a_q = (await client.get("/api/approvals", headers=self.A)).json()
        b_q = (await client.get("/api/approvals", headers=self.B)).json()
        assert [x["id"] for x in a_q] == [a_id]
        assert [x["id"] for x in b_q] == [b_id]
        # The params are the sensitive part — B's queue must not carry A's SQL.
        assert all("apr-gw-a" not in str(x["params"]) for x in b_q)

    async def test_audit_history_is_scoped(self, client):
        a_id, b_id = await self._both(client)
        await client.post(f"/api/approvals/{a_id}/decision", headers=self.A,
                          json={"decision": "approve", "decided_by": "a-operator"})
        b_all = (await client.get("/api/approvals/all", headers=self.B)).json()
        assert [x["id"] for x in b_all] == [b_id]
        # The reviewer's identity doesn't leak either.
        assert all(x["decided_by"] != "a-operator" for x in b_all)

    async def test_cross_org_read_is_404(self, client):
        a_id, _ = await self._both(client)
        assert (await client.get(f"/api/approvals/{a_id}", headers=self.B)).status_code == 404
        assert (await client.get(f"/api/approvals/{a_id}", headers=self.A)).status_code == 200

    async def test_cross_org_cannot_approve(self, client):
        """The governance bypass: approving another org's blocked call would let
        the gateway execute it on the strength of a foreign tenant's decision."""
        a_id, _ = await self._both(client)
        r = await client.post(f"/api/approvals/{a_id}/decision", headers=self.B,
                              json={"decision": "approve", "decided_by": "attacker"})
        assert r.status_code == 404
        # Still pending for the real owner — the foreign decision changed nothing.
        assert (await client.get(f"/api/approvals/{a_id}", headers=self.A)).json()["status"] == "pending"

    async def test_gateway_resume_check_works_without_a_token(self, client):
        """The gateway polls by id with no user token; scoping must not break it."""
        from control_plane.routers.approvals import approval_status

        a_id, _ = await self._both(client)
        await client.post(f"/api/approvals/{a_id}/decision", headers=self.A,
                          json={"decision": "approve"})
        r = await client.get(f"/api/approvals/{a_id}")     # no Authorization header
        assert r.status_code == 200 and r.json()["status"] == "approved"
        assert approval_status(a_id) == "approved"          # in-process helper too

    async def test_unregistered_gateway_files_under_default(self, client):
        """Demo posture: an approval from a gateway we don't know is still queued
        (default org) rather than dropped — a dropped intervene call would hang
        the agent forever."""
        aid = await self._pending_for(client, "never-registered")
        assert [x["id"] for x in (await client.get("/api/approvals")).json()] == [aid]
        assert (await client.get("/api/approvals", headers=self.A)).json() == []
