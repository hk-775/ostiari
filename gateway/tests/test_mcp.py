"""Tests for MCP integration — embedded and remote modes."""

import pytest
from starlette.testclient import TestClient

from ostiari_sidecar.mcp.manager import MCPManager
from ostiari_sidecar.mcp.models import MCPServerConfig, MCPTool


class FakeMCPServer:
    """A fake MCP server for testing embedded mode."""

    def __init__(self, **config):
        self._config = config
        self._initialized = False

    async def initialize(self):
        self._initialized = True
        return {"name": "fake-server", "version": "1.0"}

    def list_tools(self):
        return [
            {"name": "greet", "description": "Say hello", "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
            {"name": "add", "description": "Add two numbers", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
            {"name": "dangerous", "description": "A dangerous tool", "inputSchema": {}},
        ]

    async def call_tool(self, name, arguments):
        if name == "greet":
            return {"content": f"Hello, {arguments.get('name', 'world')}!"}
        elif name == "add":
            return {"content": str(arguments.get("a", 0) + arguments.get("b", 0))}
        elif name == "dangerous":
            return {"content": "executed dangerous action"}
        return {"error": f"Unknown tool: {name}"}

    async def close(self):
        pass


# Monkey-patch the embedded client to use our fake server
class FakeEmbeddedClient:
    def __init__(self, config):
        self._config = config
        self._server = FakeMCPServer(**config.config)

    async def initialize(self):
        return await self._server.initialize()

    async def list_tools(self):
        return self._server.list_tools()

    async def call_tool(self, name, arguments):
        return await self._server.call_tool(name, arguments)

    async def close(self):
        await self._server.close()


class TestMCPManager:
    @pytest.fixture
    def manager(self):
        return MCPManager()

    @pytest.mark.asyncio
    async def test_add_embedded_server(self, manager, monkeypatch):
        monkeypatch.setattr(
            "ostiari_sidecar.mcp.manager.MCPManager._create_client",
            lambda self, config: FakeEmbeddedClient(config),
        )

        config = MCPServerConfig(name="test-server", mode="embedded", package="fake-pkg")
        result = await manager.add_server(config)

        assert result["status"] == "connected"
        assert result["tools_discovered"] == 3
        assert "test-server.greet" in result["tools"]
        assert "test-server.add" in result["tools"]

    @pytest.mark.asyncio
    async def test_tool_filtering_allowed(self, manager, monkeypatch):
        monkeypatch.setattr(
            "ostiari_sidecar.mcp.manager.MCPManager._create_client",
            lambda self, config: FakeEmbeddedClient(config),
        )

        config = MCPServerConfig(
            name="filtered", mode="embedded", package="fake-pkg",
            allowed_tools=["greet", "add"],  # only expose these
        )
        result = await manager.add_server(config)

        assert result["tools_discovered"] == 2
        assert "filtered.greet" in result["tools"]
        assert "filtered.dangerous" not in result["tools"]

    @pytest.mark.asyncio
    async def test_tool_filtering_blocked(self, manager, monkeypatch):
        monkeypatch.setattr(
            "ostiari_sidecar.mcp.manager.MCPManager._create_client",
            lambda self, config: FakeEmbeddedClient(config),
        )

        config = MCPServerConfig(
            name="blocked", mode="embedded", package="fake-pkg",
            blocked_tools=["dangerous"],
        )
        result = await manager.add_server(config)

        assert result["tools_discovered"] == 2
        assert "blocked.dangerous" not in result["tools"]

    @pytest.mark.asyncio
    async def test_call_mcp_tool(self, manager, monkeypatch):
        monkeypatch.setattr(
            "ostiari_sidecar.mcp.manager.MCPManager._create_client",
            lambda self, config: FakeEmbeddedClient(config),
        )

        config = MCPServerConfig(name="math", mode="embedded", package="fake-pkg")
        await manager.add_server(config)

        result = await manager.call_tool("math.add", {"a": 3, "b": 7})
        assert result["content"] == "10"

        result = await manager.call_tool("math.greet", {"name": "Alice"})
        assert result["content"] == "Hello, Alice!"

    @pytest.mark.asyncio
    async def test_call_unknown_tool(self, manager):
        result = await manager.call_tool("nonexistent.tool", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_remove_server(self, manager, monkeypatch):
        monkeypatch.setattr(
            "ostiari_sidecar.mcp.manager.MCPManager._create_client",
            lambda self, config: FakeEmbeddedClient(config),
        )

        config = MCPServerConfig(name="removable", mode="embedded", package="fake-pkg")
        await manager.add_server(config)
        assert manager.has_tool("removable.greet")

        await manager.remove_server("removable")
        assert not manager.has_tool("removable.greet")

    @pytest.mark.asyncio
    async def test_list_servers(self, manager, monkeypatch):
        monkeypatch.setattr(
            "ostiari_sidecar.mcp.manager.MCPManager._create_client",
            lambda self, config: FakeEmbeddedClient(config),
        )

        await manager.add_server(MCPServerConfig(name="s1", mode="embedded", package="fake"))
        await manager.add_server(MCPServerConfig(name="s2", mode="embedded", package="fake"))

        servers = manager.list_servers()
        assert len(servers) == 2
        names = [s["name"] for s in servers]
        assert "s1" in names
        assert "s2" in names

    @pytest.mark.asyncio
    async def test_custom_prefix(self, manager, monkeypatch):
        monkeypatch.setattr(
            "ostiari_sidecar.mcp.manager.MCPManager._create_client",
            lambda self, config: FakeEmbeddedClient(config),
        )

        config = MCPServerConfig(name="myserver", mode="embedded", package="fake", prefix="gh")
        result = await manager.add_server(config)

        assert "gh.greet" in result["tools"]
        assert manager.has_tool("gh.greet")


class TestMCPServerEndpoints:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setattr(
            "ostiari_sidecar.mcp.manager.MCPManager._create_client",
            lambda self, config: FakeEmbeddedClient(config),
        )
        from ostiari_sidecar.server import create_app
        app = create_app()
        return TestClient(app)

    def test_add_mcp_server_via_api(self, client):
        resp = client.post("/config/mcp-servers", json={
            "name": "test-mcp",
            "mode": "embedded",
            "package": "fake-pkg",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "connected"
        assert data["tools_discovered"] == 3

    def test_list_mcp_servers(self, client):
        client.post("/config/mcp-servers", json={"name": "s1", "mode": "embedded", "package": "x"})
        resp = client.get("/config/mcp-servers")
        assert resp.status_code == 200
        assert len(resp.json()["servers"]) == 1

    def test_call_mcp_tool_via_tool_endpoint(self, client):
        # Register MCP server
        client.post("/config/mcp-servers", json={"name": "calc", "mode": "embedded", "package": "x"})

        # Call MCP tool through the same /tool/ endpoint
        resp = client.post("/tool/calc.add", json={"a": 5, "b": 3})
        assert resp.status_code == 200
        data = resp.json()
        assert data["result"]["content"] == "8"
        assert data["action"] == "calc.add"

    def test_tools_endpoint_shows_mcp_tools(self, client):
        client.post("/config/mcp-servers", json={"name": "svc", "mode": "embedded", "package": "x"})
        resp = client.get("/tools")
        data = resp.json()
        assert len(data["mcp_tools"]) == 3
        assert any(t["name"] == "svc.greet" for t in data["mcp_tools"])

    def test_remove_mcp_server_via_api(self, client):
        client.post("/config/mcp-servers", json={"name": "rm-me", "mode": "embedded", "package": "x"})
        resp = client.delete("/config/mcp-servers/rm-me")
        assert resp.status_code == 200
        assert resp.json()["status"] == "removed"

        # Tools should be gone
        resp = client.get("/tools")
        assert len(resp.json()["mcp_tools"]) == 0

    def test_health_shows_mcp_info(self, client):
        client.post("/config/mcp-servers", json={"name": "h1", "mode": "embedded", "package": "x"})
        resp = client.get("/health")
        data = resp.json()
        assert data["mcp_tools"] == 3
        assert data["mcp_servers"] == 1
