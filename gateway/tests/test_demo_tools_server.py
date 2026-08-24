"""The deployed demo service exposes working remote MCP and A2A endpoints."""

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient


def _client(monkeypatch) -> TestClient:
    monkeypatch.setenv(
        "OSTIARI_DEMO_A2A_BASE_URL",
        "http://demo-tools.evaluation.ostiari.local:9300",
    )
    source = Path(__file__).resolve().parents[1] / "demo_tools_server.py"
    spec = importlib.util.spec_from_file_location("demo_tools_server_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return TestClient(module.app)


def test_remote_mcp_lists_and_calls_tools(monkeypatch):
    client = _client(monkeypatch)

    initialized = client.post(
        "/mcp/drawio",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    )
    assert initialized.status_code == 200

    listed = client.post(
        "/mcp/drawio",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ).json()
    assert {tool["name"] for tool in listed["result"]["tools"]} >= {
        "list_diagrams",
        "create_diagram",
    }

    called = client.post(
        "/mcp/filesystem",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "read_text_file",
                # Keep this aligned with the dashboard's safe MCP demo call.
                "arguments": {
                    "path": "/tmp/ostiari-mcp-sandbox/README.txt",
                },
            },
        },
    ).json()
    assert "Ostiari demo sandbox" in called["result"]["content"][0]["text"]


def test_a2a_agent_card_and_task(monkeypatch):
    client = _client(monkeypatch)
    card = client.get("/.well-known/agent.json").json()
    assert card["url"].endswith("/a2a")
    assert {skill["id"] for skill in card["skills"]} == {
        "deploy",
        "rollback",
        "status",
    }

    response = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": "request-1",
            "method": "tasks/send",
            "params": {
                "id": "task-1",
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "Deploy auth-service"}],
                },
            },
        },
    ).json()
    assert response["result"]["status"]["state"] == "completed"
    assert "Deployment completed" in response["result"]["history"][1]["parts"][0]["text"]
