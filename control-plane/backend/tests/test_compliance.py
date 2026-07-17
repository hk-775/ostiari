"""Tests for the compliance report generator and API."""

import pytest

from control_plane import compliance
from control_plane.compliance import Evidence

pytestmark = pytest.mark.anyio


# ─── Mapping logic (unit) ────────────────────────────────────────────────────

class TestComplianceMapping:
    def test_frameworks_listed(self):
        assert "eu-ai-act" in compliance.list_frameworks()

    def test_unknown_framework_raises(self):
        with pytest.raises(ValueError):
            compliance.generate_report("bogus", Evidence())

    def test_all_met_is_green(self):
        ev = Evidence(audit_count=10, trace_count=20, blocked_count=3,
                      intervene_count=2, policy_count=4, scored_trace_count=20)
        r = compliance.generate_report("eu-ai-act", ev)
        assert r["posture"] == "green"
        assert r["score_pct"] == 100.0
        assert all(req["status"] == "met" for req in r["requirements"])

    def test_no_evidence_is_red(self):
        r = compliance.generate_report("eu-ai-act", Evidence())
        assert r["posture"] == "red"
        assert r["summary"]["unmet"] == 3

    def test_partial_human_oversight_is_yellow(self):
        # policies + scoring + records present, blocking active, but no HITL
        ev = Evidence(audit_count=5, trace_count=5, blocked_count=2,
                      intervene_count=0, policy_count=2, scored_trace_count=5)
        r = compliance.generate_report("eu-ai-act", ev)
        art14 = next(x for x in r["requirements"] if x["id"] == "art-14")
        assert art14["status"] == "partial"
        assert r["posture"] == "yellow"

    def test_build_evidence_counts_tiers(self):
        traces = [
            {"tier": "block", "score": 90},
            {"tier": "intervene", "score": 60},
            {"tier": "allow", "score": 5},
            {"tier": "allow"},  # no score
        ]
        ev = compliance.build_evidence(audit_logs=[1, 2], traces=traces, policy_count=3)
        assert ev.trace_count == 4
        assert ev.blocked_count == 1
        assert ev.intervene_count == 1
        assert ev.scored_trace_count == 3
        assert ev.audit_count == 2 and ev.policy_count == 3


# ─── API endpoint (integration) ──────────────────────────────────────────────

class TestComplianceAPI:
    async def test_frameworks_endpoint(self, client):
        r = await client.get("/api/compliance/frameworks")
        assert r.status_code == 200
        assert "eu-ai-act" in r.json()["frameworks"]

    async def test_report_empty_env_is_red(self, client):
        r = await client.get("/api/compliance/report?framework=eu-ai-act")
        assert r.status_code == 200
        body = r.json()
        assert body["framework"] == "eu-ai-act"
        assert body["posture"] == "red"  # nothing configured yet
        assert len(body["requirements"]) == 3

    async def test_report_unknown_framework_400(self, client):
        r = await client.get("/api/compliance/report?framework=nope")
        assert r.status_code == 400

    async def test_report_reflects_activity(self, client):
        # Seed a policy + traces (record-keeping + risk + oversight evidence).
        await client.post("/api/policies", json={"name": "p", "content": {"rules": []}})
        for t in [
            {"tier": "block", "score": 95, "action": "wipe"},
            {"tier": "intervene", "score": 65, "action": "email"},
            {"tier": "allow", "score": 5, "action": "read"},
        ]:
            await client.post("/api/traces/ingest", json=t)
        r = (await client.get("/api/compliance/report?framework=eu-ai-act")).json()
        assert r["evidence"]["policy_count"] >= 1
        assert r["evidence"]["blocked_count"] >= 1
        assert r["evidence"]["intervene_count"] >= 1
        # human-oversight requirement should now be met (intervene present)
        art14 = next(x for x in r["requirements"] if x["id"] == "art-14")
        assert art14["status"] == "met"
