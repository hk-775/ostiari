"""Unit tests for the proxy sidecar module."""

from __future__ import annotations

import pytest

from ostiari.proxy.registry import ToolRegistry


class TestToolRegistry:
    def test_register_and_get(self):
        registry = ToolRegistry()
        registry.register("echo", lambda p: p)
        assert registry.has("echo")
        assert registry.get("echo") is not None
        assert registry.get("nope") is None

    def test_list_tools(self):
        registry = ToolRegistry()
        registry.register("b_tool", lambda p: None)
        registry.register("a_tool", lambda p: None)
        assert registry.list_tools() == ["a_tool", "b_tool"]

    def test_from_config(self, tmp_path):
        config = tmp_path / "tools.yaml"
        config.write_text("tools:\n  json_dumps:\n    module: json\n    function: dumps\n")
        registry = ToolRegistry.from_config(config)
        assert registry.has("json_dumps")
        fn = registry.get("json_dumps")
        assert fn is not None
        assert fn({"a": 1}) == '{"a": 1}'

    def test_from_config_missing_module(self, tmp_path):
        config = tmp_path / "tools.yaml"
        config.write_text(
            "tools:\n  bad_tool:\n    module: nonexistent_module_xyz\n    function: nope\n"
        )
        registry = ToolRegistry.from_config(config)
        assert not registry.has("bad_tool")


class TestProxyServer:
    @pytest.fixture
    def client(self):
        from starlette.testclient import TestClient

        from ostiari.proxy.server import create_app

        registry = ToolRegistry()
        registry.register("echo", lambda p: {"echoed": p})
        registry.register("fail", lambda p: (_ for _ in ()).throw(ValueError("boom")))

        app = create_app(policy_path="nonexistent.yaml", registry=registry)
        return TestClient(app)

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["tools"] == 2

    def test_list_tools(self, client):
        resp = client.get("/tools")
        assert resp.status_code == 200
        assert "echo" in resp.json()["tools"]
        assert "fail" in resp.json()["tools"]

    def test_proxy_tool_allowed(self, client):
        resp = client.post("/tool/echo", json={"msg": "hello"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"] == {"echoed": {"msg": "hello"}}
        assert data["action"] == "echo"
        assert "duration_ms" in data

    def test_proxy_tool_unknown(self, client):
        resp = client.post("/tool/nonexistent", json={})
        assert resp.status_code == 404
        assert "Unknown tool" in resp.json()["error"]

    def test_validate_only_allowed(self, client):
        resp = client.post("/validate", json={"action": "echo", "params": {"x": 1}})
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed"] is True
        assert data["tier"] == "allow"

    def test_validate_only_missing_action(self, client):
        resp = client.post("/validate", json={"params": {}})
        assert resp.status_code == 400

    def test_proxy_tool_blocked(self):
        """Test that a tool call is blocked when policy blocks it."""
        from starlette.testclient import TestClient

        from ostiari.proxy.server import create_app

        registry = ToolRegistry()
        registry.register("delete_all", lambda p: None)

        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("block:\n  - delete_all\n")
            policy_path = f.name

        app = create_app(policy_path=policy_path, registry=registry)
        client = TestClient(app)

        resp = client.post("/tool/delete_all", json={"table": "users"})
        assert resp.status_code == 403
        data = resp.json()
        assert data["blocked"] is True
        assert data["action"] == "delete_all"

        Path(policy_path).unlink()
