"""Authentication and tenant isolation for the live-trace WebSocket."""

from __future__ import annotations

import pytest
from fastapi import WebSocketDisconnect, WebSocketException, status

pytestmark = pytest.mark.anyio


class _FakeWebSocket:
    def __init__(self, *, query_params: dict[str, str] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.query_params = query_params or {}
        self.headers = headers or {}
        self.accepted = False
        self.sent: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, value: dict) -> None:
        self.sent.append(value)

    async def receive_text(self) -> str:
        raise WebSocketDisconnect(code=1000)


class TestTraceWebSocketAuth:
    async def test_missing_token_is_rejected_before_accept(self, monkeypatch):
        from control_plane.routers import traces

        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        websocket = _FakeWebSocket()

        with pytest.raises(WebSocketException) as exc:
            await traces.websocket_traces(websocket)  # type: ignore[arg-type]

        assert exc.value.code == status.WS_1008_POLICY_VIOLATION
        assert websocket.accepted is False

    async def test_invalid_token_is_rejected_before_accept(self, monkeypatch):
        from control_plane.routers import traces

        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        websocket = _FakeWebSocket(query_params={"token": "not-a-jwt"})

        with pytest.raises(WebSocketException) as exc:
            await traces.websocket_traces(websocket)  # type: ignore[arg-type]

        assert exc.value.code == status.WS_1008_POLICY_VIOLATION
        assert websocket.accepted is False

    async def test_valid_token_receives_only_its_org_history(self, monkeypatch):
        from control_plane.auth.service import create_access_token
        from control_plane.routers import traces

        monkeypatch.setenv("OSTIARI_REQUIRE_AUTH", "true")
        traces._recent_traces.clear()
        traces._ws_clients.clear()
        traces._recent_traces["org-a"].append({"trace_id": "org-a-trace"})
        traces._recent_traces["default"].append({"trace_id": "default-trace"})

        token = create_access_token(
            user_id=7,
            email="operator@org-a.test",
            role="operator",
            org="org-a",
        )
        websocket = _FakeWebSocket(query_params={"token": token})

        await traces.websocket_traces(websocket)  # type: ignore[arg-type]

        assert websocket.accepted is True
        assert websocket.sent == [{"trace_id": "org-a-trace"}]
        assert websocket not in traces._ws_clients["org-a"]

    async def test_demo_mode_remains_open(self, monkeypatch):
        from control_plane.routers import traces

        monkeypatch.delenv("OSTIARI_REQUIRE_AUTH", raising=False)
        traces._recent_traces.clear()
        traces._ws_clients.clear()
        traces._recent_traces["demo-org"].append({"trace_id": "demo-trace"})
        websocket = _FakeWebSocket(query_params={"org": "demo-org"})

        await traces.websocket_traces(websocket)  # type: ignore[arg-type]

        assert websocket.accepted is True
        assert websocket.sent == [{"trace_id": "demo-trace"}]
