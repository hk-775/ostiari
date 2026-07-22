"""Tests for the tamper-evident (hash-chained) audit log + reader auth."""

import pytest

pytestmark = pytest.mark.anyio


async def _session():
    """A DB session on the (freshly-created) test database."""
    from control_plane.database import get_db
    gen = get_db()
    db = await gen.__anext__()
    return gen, db


class TestHashChain:
    async def test_entries_chain(self, app_and_db):
        from control_plane.services.audit_service import audit
        gen, db = await _session()
        try:
            e1 = await audit.log(db, "admin", "update", "policy", "p1", {"x": 1})
            e2 = await audit.log(db, "admin", "delete", "tool", "t1", {"y": 2})
            assert e1.entry_hash and e2.entry_hash
            assert e1.prev_hash == ""            # genesis
            assert e2.prev_hash == e1.entry_hash  # chained
        finally:
            await gen.aclose()

    async def test_verify_clean_chain(self, app_and_db):
        from control_plane.services.audit_service import audit
        gen, db = await _session()
        try:
            await audit.log(db, "a", "x", "r", "1", {})
            await audit.log(db, "a", "y", "r", "2", {})
            result = await audit.verify_chain(db)
            assert result["valid"] is True and result["checked"] == 2
        finally:
            await gen.aclose()

    async def test_verify_detects_content_tampering(self, app_and_db):
        from control_plane.services.audit_service import audit
        gen, db = await _session()
        try:
            e1 = await audit.log(db, "a", "x", "r", "1", {"amount": 1})
            await audit.log(db, "a", "y", "r", "2", {})
            # tamper: change a recorded detail without recomputing the hash
            e1.details = {"amount": 999999}
            await db.flush()
            result = await audit.verify_chain(db)
            assert result["valid"] is False
            assert result["broken_at_id"] == e1.id
            assert "altered" in result["reason"] or "mismatch" in result["reason"]
        finally:
            await gen.aclose()

    async def test_verify_detects_deletion(self, app_and_db):
        from control_plane.models.database import AuditLog
        from control_plane.services.audit_service import audit
        gen, db = await _session()
        try:
            await audit.log(db, "a", "x", "r", "1", {})
            e2 = await audit.log(db, "a", "y", "r", "2", {})
            await audit.log(db, "a", "z", "r", "3", {})
            # delete a middle row → the chain link from e3 back to e2 breaks
            await db.delete(e2)
            await db.flush()
            result = await audit.verify_chain(db)
            assert result["valid"] is False
        finally:
            await gen.aclose()


class TestReaderAuth:
    async def test_reader_open_in_demo(self, client, monkeypatch):
        monkeypatch.delenv("OSTIARI_REQUIRE_AUTH", raising=False)
        assert (await client.get("/api/audit")).status_code == 200

    async def test_reader_requires_auth_when_enforced(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        r = await client.get("/api/audit")
        assert r.status_code == 401

    async def test_verify_endpoint_reachable(self, client, monkeypatch):
        monkeypatch.delenv("OSTIARI_REQUIRE_AUTH", raising=False)
        r = await client.get("/api/audit/verify")
        assert r.status_code == 200
        assert r.json()["valid"] is True   # empty chain is valid
