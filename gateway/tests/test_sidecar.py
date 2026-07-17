"""Integration tests for the generic sidecar."""

import json

import pytest
from ostiari_gateway.models import PolicyConfig, SidecarConfig, ToolDefinition
from ostiari_gateway.server import create_app
from starlette.testclient import TestClient


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)


@pytest.fixture
def configured_client(httpserver):
    """Client with tools pointing to a mock HTTP server."""
    httpserver.expect_request("/send", method="POST").respond_with_json(
        {"message_id": "msg-123", "status": "sent"}
    )
    httpserver.expect_request("/query", method="POST").respond_with_json(
        {"rows": [{"id": 1, "name": "Alice"}]}
    )

    config = SidecarConfig(
        sidecar_id="test-sidecar",
        tools=[
            ToolDefinition(
                name="send_email",
                endpoint=httpserver.url_for("/send"),
                description="Send email",
            ),
            ToolDefinition(
                name="db_query",
                endpoint=httpserver.url_for("/query"),
                description="Query database",
            ),
        ],
        policy=PolicyConfig(
            allow=["db_query"],
            block=["dangerous_action"],
            rules=[{"type": "risk_adjust", "action": "send_email", "risk_adjust": 15}],
        ),
    )
    app = create_app(initial_config=config)
    return TestClient(app)


class TestHealth:
    def test_health_empty(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["tools_registered"] == 0

    def test_health_configured(self, configured_client):
        resp = configured_client.get("/health")
        data = resp.json()
        assert data["tools_registered"] == 2
        assert data["sidecar_id"] == "test-sidecar"


class TestConfigAPI:
    def test_get_empty_config(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tools"] == []

    def test_apply_full_config(self, client):
        config = {
            "sidecar_id": "my-sidecar",
            "tools": [
                {"name": "echo", "endpoint": "http://localhost:9999/echo"},
            ],
            "policy": {"block": ["bad_action"]},
        }
        resp = client.post("/config", json=config)
        assert resp.status_code == 200
        assert resp.json()["tools_registered"] == 1

        # Verify persisted
        resp = client.get("/config")
        data = resp.json()
        assert data["sidecar_id"] == "my-sidecar"
        assert len(data["tools"]) == 1

    def test_add_single_tool(self, client):
        resp = client.post(
            "/config/tools/new_tool",
            json={"endpoint": "http://localhost:9999/new", "description": "A new tool"},
        )
        assert resp.status_code == 200
        assert resp.json()["tool"] == "new_tool"

        resp = client.get("/tools")
        assert any(t["name"] == "new_tool" for t in resp.json()["tools"])

    def test_remove_tool(self, client):
        # Add then remove
        client.post("/config/tools/temp", json={"endpoint": "http://x/y"})
        resp = client.delete("/config/tools/temp")
        assert resp.status_code == 200
        assert resp.json()["removed"] is True

        resp = client.delete("/config/tools/nonexistent")
        assert resp.status_code == 404

    def test_apply_policy(self, client):
        resp = client.post("/config/policy", json={"block": ["*.delete"], "allow": ["read"]})
        assert resp.status_code == 200
        assert resp.json()["policy_applied"] is True


class TestToolProxy:
    def test_tool_allowed_and_proxied(self, configured_client):
        resp = configured_client.post("/tool/db_query", json={"sql": "SELECT * FROM users"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "db_query"
        assert data["result"] == {"rows": [{"id": 1, "name": "Alice"}]}

    def test_tool_blocked(self, configured_client):
        # First register the tool
        configured_client.post(
            "/config/tools/dangerous_action",
            json={"endpoint": "http://localhost:9999/x"},
        )
        resp = configured_client.post("/tool/dangerous_action", json={"target": "all"})
        assert resp.status_code == 403
        data = resp.json()
        assert data["blocked"] is True

    def test_unknown_tool(self, configured_client):
        resp = configured_client.post("/tool/nonexistent", json={})
        assert resp.status_code == 404

    def test_validate_only(self, configured_client):
        resp = configured_client.post(
            "/validate", json={"action": "db_query", "params": {"sql": "SELECT 1"}}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["allowed"] is True
        assert data["tier"] == "allow"

    def test_otel_context_propagated_to_tool(self, httpserver):
        """Verify that traceparent header is forwarded to tool endpoints."""
        from opentelemetry import trace as otel_trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            SimpleSpanProcessor,
            SpanExporter,
            SpanExportResult,
        )
        from ostiari_gateway.server import create_app
        from werkzeug import Response as WerkzeugResponse

        # Simple in-memory exporter to capture spans
        class MemoryExporter(SpanExporter):
            def __init__(self):
                self.spans = []

            def export(self, spans):
                self.spans.extend(spans)
                return SpanExportResult.SUCCESS

        # Set up a real TracerProvider so inject() actually writes headers.
        # OpenTelemetry only honors the FIRST set_tracer_provider() per process,
        # so if another test already set one, override=True is required to make
        # this test's exporter actually receive spans (test-isolation safety).
        exporter = MemoryExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        otel_trace._TRACER_PROVIDER_SET_ONCE._done = False
        otel_trace.set_tracer_provider(provider)

        received_headers: dict[str, str] = {}

        def capture_handler(request):
            received_headers.update(dict(request.headers))
            return WerkzeugResponse(
                json.dumps({"status": "ok"}),
                status=200,
                content_type="application/json",
            )

        httpserver.expect_request("/echo", method="POST").respond_with_handler(capture_handler)

        config = SidecarConfig(
            sidecar_id="otel-test",
            tools=[
                ToolDefinition(name="echo", endpoint=httpserver.url_for("/echo")),
            ],
        )
        app = create_app(initial_config=config)
        test_client = TestClient(app)

        traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        resp = test_client.post(
            "/tool/echo",
            json={"msg": "hello"},
            headers={"traceparent": traceparent},
        )
        assert resp.status_code == 200

        # The tool endpoint should have received a traceparent header
        assert "Traceparent" in received_headers or "traceparent" in received_headers

        # Verify spans were created
        span_names = [s.name for s in exporter.spans]
        assert any("validate" in name for name in span_names)
        assert any("proxy" in name for name in span_names)


class TestRuntimeConfigEndpoints:
    """The dashboard reads/writes these gateway config endpoints."""

    def test_budget_reset_roundtrip(self, client):
        assert client.get("/config/budget-reset").json() == {"schedule": "manual"}
        r = client.post("/config/budget-reset", json={"schedule": "weekly"})
        assert r.status_code == 200
        assert client.get("/config/budget-reset").json()["schedule"] == "weekly"

    def test_task_classification_roundtrip(self, client):
        body = {"rules": [{"keyword": "code", "category": "reasoning"}],
                "model_mapping": {"reasoning": "claude-opus"}}
        assert client.post("/config/task-classification", json=body).status_code == 200
        back = client.get("/config/task-classification").json()
        assert back["rules"] == body["rules"]
        assert back["model_mapping"] == body["model_mapping"]

    def test_llm_roundtrip(self, client):
        body = {"routing_rules": [{"if": "tokens>1000", "use": "claude-sonnet"}]}
        assert client.post("/config/llm", json=body).status_code == 200
        assert client.get("/config/llm").json()["routing_rules"] == body["routing_rules"]

    def test_routing_overrides_roundtrip(self, client):
        body = {"overrides": [{"agent": "crm", "model": "gpt-4o"}]}
        assert client.post("/config/routing-overrides", json=body).status_code == 200
        assert client.get("/config/routing-overrides").json()["overrides"] == body["overrides"]
