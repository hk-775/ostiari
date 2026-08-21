"""Isolated Sandbox run lifecycle and governed tool bridge."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from control_plane.auth.service import create_access_token
from control_plane.database import async_session
from control_plane.models.database import AuditLog, SandboxRun
from sqlalchemy import select

pytestmark = pytest.mark.anyio

_DIGEST = "a" * 64


def _headers(org: str = "default", role: str = "admin") -> dict[str, str]:
    token = create_access_token(
        user_id=1,
        email=f"{role}@{org}.test",
        role=role,
        org=org,
    )
    return {"Authorization": f"Bearer {token}"}


async def _gateway(client, gateway_id: str = "sandbox-gw", headers=None):
    response = await client.post(
        "/api/gateways",
        headers=headers,
        json={
            "id": gateway_id,
            "name": gateway_id,
            "endpoint": "http://gateway.internal:8421",
            "description": "",
        },
    )
    assert response.status_code == 200, response.text


async def _start(client, gateway_id: str = "sandbox-gw", headers=None, **overrides):
    body = {
        "gateway_id": gateway_id,
        "language": "javascript",
        "source_digest": _DIGEST,
        "source_bytes": 128,
        **overrides,
    }
    return await client.post("/api/sandbox/runs", headers=headers, json=body)


class _GatewayClient:
    calls: list[dict] = []
    status = 200
    body = {"result": {"ok": True}}
    chunks: list[bytes] | None = None
    chunk_delay = 0.0

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, content=None, headers=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "content": content,
                "headers": headers or {},
                "timeout": self.timeout,
            }
        )
        chunks = self.chunks
        if chunks is None:
            chunks = [json.dumps(self.body, separators=(",", ":")).encode()]
        return _GatewayStream(
            _GatewayResponse(
                self.status,
                chunks,
                self.chunk_delay,
            )
        )


class _GatewayResponse:
    def __init__(self, status: int, chunks: list[bytes], chunk_delay: float):
        self.status_code = status
        self.headers = {"content-type": "application/json"}
        self._chunks = chunks
        self._chunk_delay = chunk_delay

    async def aiter_bytes(self, chunk_size=None):
        for chunk in self._chunks:
            if self._chunk_delay:
                await asyncio.sleep(self._chunk_delay)
            yield chunk


class _GatewayStream:
    def __init__(self, response: _GatewayResponse):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def gateway_client(monkeypatch):
    from control_plane.routers import sandbox

    _GatewayClient.calls = []
    _GatewayClient.status = 200
    _GatewayClient.body = {"result": {"ok": True}}
    _GatewayClient.chunks = None
    _GatewayClient.chunk_delay = 0.0
    monkeypatch.setattr(sandbox.httpx, "AsyncClient", _GatewayClient)
    return _GatewayClient


class TestRunLifecycle:
    async def test_create_persists_metadata_without_source(self, client):
        await _gateway(client)
        response = await _start(client)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "running"
        assert body["gateway_id"] == "sandbox-gw"
        assert body["source_digest"] == _DIGEST
        assert body["source_bytes"] == 128
        assert body["timeout_ms"] == 10_000
        assert body["max_tool_calls"] == 20
        assert "source" not in body

        async with async_session() as db:
            row = await db.get(SandboxRun, body["id"])
            assert row is not None
            assert not hasattr(row, "source")
            audit_rows = (
                (
                    await db.execute(
                        select(AuditLog).where(
                            AuditLog.resource_type == "sandbox_run",
                            AuditLog.resource_id == body["id"],
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [(item.action, item.details["source_digest"]) for item in audit_rows] == [
                ("start", _DIGEST)
            ]

    async def test_source_is_rejected_instead_of_being_stored(self, client):
        await _gateway(client)
        response = await _start(client, source="console.log('secret')")
        assert response.status_code == 422

    async def test_source_limit_is_server_owned(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_SANDBOX_MAX_SOURCE_BYTES", "1024")
        await _gateway(client)
        response = await _start(client, source_bytes=1025)
        assert response.status_code == 413
        assert "1024 byte limit" in response.json()["detail"]

    async def test_active_run_limit(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_SANDBOX_MAX_ACTIVE_RUNS", "1")
        await _gateway(client)
        first = await _start(client)
        assert first.status_code == 201
        response = await _start(client, source_digest="b" * 64)
        assert response.status_code == 429
        await client.delete(f"/api/sandbox/runs/{first.json()['id']}")
        assert (await _start(client, source_digest="c" * 64)).status_code == 201

    async def test_active_run_admission_is_atomic(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_SANDBOX_MAX_ACTIVE_RUNS", "1")
        await _gateway(client)

        responses = await asyncio.gather(*(_start(client) for _ in range(20)))
        assert [response.status_code for response in responses].count(201) == 1
        assert [response.status_code for response in responses].count(429) == 19

        async with async_session() as db:
            rows = (
                (
                    await db.execute(
                        select(SandboxRun).where(SandboxRun.status == "running")
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].active_slot == 0

    async def test_complete_clamps_output_and_is_idempotent(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_SANDBOX_MAX_OUTPUT_BYTES", "1024")
        await _gateway(client)
        run = (await _start(client)).json()
        response = await client.post(
            f"/api/sandbox/runs/{run['id']}/complete",
            json={
                "status": "completed",
                "duration_ms": 25,
                "output_bytes": 50_000,
                "error": "",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert response.json()["output_bytes"] == 1024

        replay = await client.post(
            f"/api/sandbox/runs/{run['id']}/complete",
            json={
                "status": "error",
                "duration_ms": 30,
                "output_bytes": 0,
                "error": "late error",
            },
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "completed"
        assert replay.json()["error"] == ""

        async with async_session() as db:
            actions = (
                (
                    await db.execute(
                        select(AuditLog.action)
                        .where(
                            AuditLog.resource_type == "sandbox_run",
                            AuditLog.resource_id == run["id"],
                        )
                        .order_by(AuditLog.id)
                    )
                )
                .scalars()
                .all()
            )
            assert actions == ["start", "completed"]

    async def test_cancel_marks_run_terminal(self, client):
        await _gateway(client)
        run = (await _start(client)).json()
        response = await client.delete(f"/api/sandbox/runs/{run['id']}")
        assert response.status_code == 200
        assert response.json()["status"] == "cancelled"
        assert response.json()["completed_at"] is not None

    async def test_terminal_transition_is_atomic(self, client):
        await _gateway(client)
        run = (await _start(client)).json()
        path = f"/api/sandbox/runs/{run['id']}"
        requests = [
            client.post(
                f"{path}/complete",
                json={
                    "status": "completed" if index % 2 == 0 else "error",
                    "duration_ms": 25 + index,
                    "output_bytes": index,
                    "error": "" if index % 2 == 0 else f"error-{index}",
                },
            )
            if index % 3
            else client.delete(path)
            for index in range(12)
        ]

        responses = await asyncio.gather(*requests)
        assert all(response.status_code == 200 for response in responses)
        terminal_statuses = {response.json()["status"] for response in responses}
        assert len(terminal_statuses) == 1

        async with async_session() as db:
            row = await db.get(SandboxRun, run["id"])
            assert row is not None
            assert row.status in terminal_statuses
            assert row.active_slot is None
            actions = (
                (
                    await db.execute(
                        select(AuditLog.action)
                        .where(
                            AuditLog.resource_type == "sandbox_run",
                            AuditLog.resource_id == run["id"],
                        )
                        .order_by(AuditLog.id)
                    )
                )
                .scalars()
                .all()
            )
            assert actions[0] == "start"
            assert len(actions) == 2
            assert actions[1] == row.status

    async def test_cancel_expires_stale_run_before_building_response(self, client):
        await _gateway(client)
        run = (await _start(client)).json()
        async with async_session() as db:
            row = await db.get(SandboxRun, run["id"])
            row.started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
            await db.commit()

        response = await client.delete(f"/api/sandbox/runs/{run['id']}")
        assert response.status_code == 200
        assert response.json()["status"] == "timed_out"
        assert response.json()["error"] == "Execution deadline elapsed"

    async def test_get_lazily_expires_stale_run(self, client, monkeypatch):
        monkeypatch.setenv("OSTIARI_SANDBOX_TIMEOUT_MS", "1000")
        await _gateway(client)
        run = (await _start(client)).json()
        async with async_session() as db:
            row = await db.get(SandboxRun, run["id"])
            row.started_at = datetime.now(timezone.utc) - timedelta(seconds=2)
            await db.commit()

        response = await client.get(f"/api/sandbox/runs/{run['id']}")
        assert response.status_code == 200
        assert response.json()["status"] == "timed_out"
        async with async_session() as db:
            actions = (
                (
                    await db.execute(
                        select(AuditLog.action)
                        .where(
                            AuditLog.resource_type == "sandbox_run",
                            AuditLog.resource_id == run["id"],
                        )
                        .order_by(AuditLog.id)
                    )
                )
                .scalars()
                .all()
            )
            assert actions == ["start", "timed_out"]


class TestToolBridge:
    async def test_proxies_with_controlled_headers_and_caller_token(
        self, client, gateway_client, admin_headers
    ):
        await _gateway(client, headers=admin_headers)
        run = (await _start(client, headers=admin_headers)).json()
        gateway_client.status = 403
        gateway_client.body = {"reason": "policy"}

        response = await client.post(
            f"/api/sandbox/runs/{run['id']}/tools/db_delete",
            headers={**admin_headers, "X-Actor": "spoofed", "X-Secret": "do-not-forward"},
            json={"table": "users"},
        )
        assert response.status_code == 403
        assert response.json() == {"reason": "policy"}
        call = gateway_client.calls[-1]
        assert call["url"] == "http://gateway.internal:8421/tool/db_delete"
        assert call["headers"] == {
            "Content-Type": "application/json",
            "X-Agent-Id": "1",
            "X-Session-Id": f"sandbox-code:{run['id']}",
            "X-Plan": "Sandbox code execution",
            "X-Step": "1/20",
            "Authorization": admin_headers["Authorization"],
        }
        assert call["content"] == b'{"table":"users"}'
        assert call["timeout"] <= 10

    async def test_downstream_401_is_not_returned_as_session_401(
        self, client, gateway_client, admin_headers
    ):
        await _gateway(client, headers=admin_headers)
        run = (await _start(client, headers=admin_headers)).json()
        gateway_client.status = 401
        gateway_client.body = {"error": "authentication required"}

        response = await client.post(
            f"/api/sandbox/runs/{run['id']}/tools/db_query",
            headers=admin_headers,
            json={},
        )
        assert response.status_code == 502
        assert response.json() == {
            "detail": "Gateway rejected Sandbox credentials",
            "gateway_status": 401,
        }
        assert gateway_client.calls[-1]["headers"]["Authorization"] == admin_headers[
            "Authorization"
        ]

    async def test_dedicated_gateway_token_overrides_caller_token(
        self, client, gateway_client, admin_headers, monkeypatch
    ):
        monkeypatch.setenv("OSTIARI_SANDBOX_GATEWAY_TOKEN", "service-token")
        monkeypatch.setenv("OSTIARI_SANDBOX_GATEWAY_AGENT_ID", "sandbox-service")
        await _gateway(client, headers=admin_headers)
        run = (await _start(client, headers=admin_headers)).json()

        response = await client.post(
            f"/api/sandbox/runs/{run['id']}/tools/db_query",
            headers=admin_headers,
            json={},
        )
        assert response.status_code == 200
        headers = gateway_client.calls[-1]["headers"]
        assert headers["Authorization"] == "Bearer service-token"
        assert headers["X-Agent-Id"] == "sandbox-service"

    async def test_gateway_response_size_is_bounded(
        self, client, gateway_client, monkeypatch
    ):
        monkeypatch.setenv("OSTIARI_SANDBOX_MAX_OUTPUT_BYTES", "1024")
        await _gateway(client)
        run = (await _start(client)).json()
        gateway_client.chunks = [b"x" * 700, b"y" * 700]

        response = await client.post(
            f"/api/sandbox/runs/{run['id']}/tools/db_query",
            json={},
        )
        assert response.status_code == 502
        assert response.json()["detail"] == "Gateway response exceeds 1024 byte limit"

    async def test_gateway_stream_has_absolute_run_deadline(
        self, client, gateway_client, monkeypatch
    ):
        monkeypatch.setenv("OSTIARI_SANDBOX_TIMEOUT_MS", "1000")
        await _gateway(client)
        run = (await _start(client)).json()
        async with async_session() as db:
            row = await db.get(SandboxRun, run["id"])
            row.started_at = datetime.now(timezone.utc) - timedelta(milliseconds=400)
            await db.commit()
        gateway_client.chunks = [b"x"] * 5
        gateway_client.chunk_delay = 0.2

        response = await client.post(
            f"/api/sandbox/runs/{run['id']}/tools/db_query",
            json={},
        )
        assert response.status_code == 504
        current = await client.get(f"/api/sandbox/runs/{run['id']}")
        assert current.json()["status"] == "timed_out"

    async def test_tool_count_is_atomic_and_limited(self, client, gateway_client, monkeypatch):
        monkeypatch.setenv("OSTIARI_SANDBOX_MAX_TOOL_CALLS", "2")
        await _gateway(client)
        run = (await _start(client)).json()
        path = f"/api/sandbox/runs/{run['id']}/tools/db_query"
        responses = await asyncio.gather(*(client.post(path, json={}) for _ in range(5)))
        assert sorted(response.status_code for response in responses) == [
            200,
            200,
            429,
            429,
            429,
        ]
        assert len(gateway_client.calls) == 2

        current = await client.get(f"/api/sandbox/runs/{run['id']}")
        assert current.json()["tool_calls"] == 2

    async def test_payload_limit_blocks_before_gateway(self, client, gateway_client, monkeypatch):
        monkeypatch.setenv("OSTIARI_SANDBOX_MAX_TOOL_PAYLOAD_BYTES", "256")
        await _gateway(client)
        run = (await _start(client)).json()
        response = await client.post(
            f"/api/sandbox/runs/{run['id']}/tools/db_query",
            json={"value": "x" * 300},
        )
        assert response.status_code == 413
        assert gateway_client.calls == []
        current = await client.get(f"/api/sandbox/runs/{run['id']}")
        assert current.json()["tool_calls"] == 0

    async def test_terminal_run_cannot_execute_more_tools(self, client, gateway_client):
        await _gateway(client)
        run = (await _start(client)).json()
        await client.delete(f"/api/sandbox/runs/{run['id']}")
        response = await client.post(
            f"/api/sandbox/runs/{run['id']}/tools/db_query",
            json={},
        )
        assert response.status_code == 409
        assert gateway_client.calls == []

    async def test_expired_run_fails_before_gateway(self, client, gateway_client, monkeypatch):
        monkeypatch.setenv("OSTIARI_SANDBOX_TIMEOUT_MS", "1000")
        await _gateway(client)
        run = (await _start(client)).json()
        async with async_session() as db:
            row = await db.get(SandboxRun, run["id"])
            row.started_at = datetime.now(timezone.utc) - timedelta(seconds=2)
            await db.commit()

        response = await client.post(
            f"/api/sandbox/runs/{run['id']}/tools/db_query",
            json={},
        )
        assert response.status_code == 410
        assert gateway_client.calls == []


class TestSecurityBoundary:
    @pytest.mark.usefixtures("multi_tenant_mode")
    async def test_cross_tenant_gateway_and_run_are_hidden(self, client):
        org_a = _headers("org-a")
        org_b = _headers("org-b")
        await _gateway(client, "tenant-gw", headers=org_a)
        assert (await _start(client, "tenant-gw", headers=org_b)).status_code == 404

        run = (await _start(client, "tenant-gw", headers=org_a)).json()
        assert (
            await client.get(f"/api/sandbox/runs/{run['id']}", headers=org_b)
        ).status_code == 404
        assert (
            await client.post(
                f"/api/sandbox/runs/{run['id']}/tools/db_query",
                headers=org_b,
                json={},
            )
        ).status_code == 404

    async def test_viewer_cannot_start_or_drive_runs(
        self, client, monkeypatch, admin_headers, viewer_headers
    ):
        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        await _gateway(client, headers=admin_headers)
        assert (await _start(client, headers=viewer_headers)).status_code == 403

        run = (await _start(client, headers=admin_headers)).json()
        response = await client.post(
            f"/api/sandbox/runs/{run['id']}/tools/db_query",
            headers=viewer_headers,
            json={},
        )
        assert response.status_code == 403
