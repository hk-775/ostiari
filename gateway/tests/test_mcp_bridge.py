"""Tests for the MCP bridge — exposes gateway tools over the MCP JSON-RPC protocol."""

from __future__ import annotations

import httpx
import pytest
from ostiari_gateway.mcp_bridge import create_bridge_app
from starlette.testclient import TestClient


@pytest.fixture
def make_bridge(monkeypatch):
    """Factory: MCP bridge whose upstream gateway HTTP calls are served by `handler`.

    Uses monkeypatch so the patched httpx.AsyncClient is restored after the test
    (a bare global assignment would pollute every later test in the session).
    """
    def _factory(handler) -> TestClient:
        class _Client(httpx.AsyncClient):
            def __init__(self, *a, **k):
                k["transport"] = httpx.MockTransport(handler)
                super().__init__(*a, **k)

        import ostiari_gateway.mcp_bridge as mod
        monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
        return TestClient(create_bridge_app("http://gw.local"))

    return _factory


def _gateway_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/tools":
        return httpx.Response(200, json={"tools": [
            {"name": "getOrder", "description": "Fetch an order",
             "schema": {"type": "object", "properties": {"id": {"type": "string"}}}},
        ]})
    if request.url.path == "/tool/getOrder":
        return httpx.Response(200, json={"result": {"id": "1", "status": "open"}})
    if request.url.path == "/tool/blocked_tool":
        return httpx.Response(403, json={"reason": "risk score 82 exceeds block threshold"})
    return httpx.Response(404, json={"error": "not found"})


class TestMCPBridge:
    def test_initialize(self, make_bridge):
        c = make_bridge(_gateway_handler)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        body = r.json()
        assert body["result"]["protocolVersion"]
        assert body["result"]["serverInfo"]["name"] == "ostiari-bridge"
        assert "tools" in body["result"]["capabilities"]

    def test_initialized_notification_no_response(self, make_bridge):
        c = make_bridge(_gateway_handler)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code == 202

    def test_tools_list(self, make_bridge):
        c = make_bridge(_gateway_handler)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = r.json()["result"]["tools"]
        assert tools[0]["name"] == "getOrder"
        assert tools[0]["inputSchema"]["properties"]["id"]

    def test_tools_call_forwards_and_returns_content(self, make_bridge):
        c = make_bridge(_gateway_handler)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "getOrder", "arguments": {"id": "1"}}})
        result = r.json()["result"]
        assert result["isError"] is False
        assert "open" in result["content"][0]["text"]

    def test_tools_call_surfaces_governance_block(self, make_bridge):
        c = make_bridge(_gateway_handler)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                 "params": {"name": "blocked_tool", "arguments": {}}})
        result = r.json()["result"]
        assert result["isError"] is True
        assert "BLOCKED by Ostiari" in result["content"][0]["text"]
        assert "risk score 82" in result["content"][0]["text"]

    def test_unknown_method(self, make_bridge):
        c = make_bridge(_gateway_handler)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "does/notExist"})
        assert r.json()["error"]["code"] == -32601
