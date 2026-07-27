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

    def test_malformed_json_body_is_400(self, configured_client):
        """A bad body is the caller's error, not a gateway fault. Unguarded, the
        decode failure escaped as an unhandled exception on the gateway's hottest
        path — a 500 plus a stack trace, where the agent needs an actionable 400."""
        resp = configured_client.post("/tool/db_query", content=b"{not json",
                                      headers={"Content-Type": "application/json"})
        assert resp.status_code == 400
        assert "Malformed JSON" in resp.json()["error"]

    def test_non_object_body_is_400(self, configured_client):
        # Tool params are keyword arguments; a bare scalar or list can't be one.
        resp = configured_client.post("/tool/db_query", json="just a string")
        assert resp.status_code == 400
        resp = configured_client.post("/tool/db_query", json=["a", "b"])
        assert resp.status_code == 400

    def test_bad_body_is_rejected_before_the_tool_runs(self, configured_client):
        """The 400 must come from the gateway, not from a tool that already ran
        with garbage — rejecting after execution would be a side effect on an
        invalid request."""
        resp = configured_client.post("/tool/db_query", json=["not", "params"])
        assert resp.status_code == 400
        assert "result" not in resp.json()

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


class TestShadowMode:
    """Shadow mode: evaluate policy but never block or run real tools."""

    def _shadow_client(self, httpserver):
        # A tool whose backend, if hit, records the call — so we can prove it ISN'T hit.
        httpserver.expect_request("/run", method="POST").respond_with_json({"ran": True})
        config = SidecarConfig(
            sidecar_id="shadow-sc",
            mode="shadow",
            tools=[ToolDefinition(name="send_email", endpoint=httpserver.url_for("/run"))],
            policy=PolicyConfig(block=["dangerous_action"], allow=["send_email"]),
        )
        return TestClient(create_app(initial_config=config)), httpserver

    def test_mode_endpoint_roundtrip(self, client):
        assert client.get("/config/mode").json()["mode"] == "enforce"
        assert client.post("/config/mode", json={"mode": "shadow"}).status_code == 200
        assert client.get("/config/mode").json()["mode"] == "shadow"

    def test_mode_endpoint_rejects_invalid(self, client):
        assert client.post("/config/mode", json={"mode": "bogus"}).status_code == 400

    def _blocking_client(self, mode):
        config = SidecarConfig(
            sidecar_id="sc",
            mode=mode,
            tools=[ToolDefinition(name="dangerous_action", endpoint="http://localhost:9999/x")],
            policy=PolicyConfig(block=["dangerous_action"]),
        )
        return TestClient(create_app(initial_config=config))

    def test_blocked_action_not_blocked_in_shadow(self):
        client = self._blocking_client("shadow")
        resp = client.post("/tool/dangerous_action", json={"target": "all"})
        assert resp.status_code == 200  # NOT 403
        body = resp.json()
        assert body["shadow"] is True
        assert body["would_block"] is True

    def test_enforce_mode_still_blocks(self):
        # Sanity: default enforce mode blocks the same action.
        client = self._blocking_client("enforce")
        resp = client.post("/tool/dangerous_action", json={"target": "all"})
        assert resp.status_code == 403

    def test_allowed_tool_not_really_executed_in_shadow(self, httpserver):
        client, hs = self._shadow_client(httpserver)
        resp = client.post("/tool/send_email", json={"to": "x@y.io"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["shadow"] is True
        assert body["would_block"] is False
        assert body["result"]["note"].startswith("shadowed")
        # The real backend must NOT have been called.
        assert len(hs.log) == 0

    def test_agent_auth_block_shadowed(self, httpserver):
        # Restrict agent auth so the call would be denied, then shadow it.
        httpserver.expect_request("/run", method="POST").respond_with_json({"ran": True})
        config = SidecarConfig(
            sidecar_id="sc",
            mode="shadow",
            tools=[ToolDefinition(name="send_email", endpoint=httpserver.url_for("/run"))],
            agent_auth={
                "enabled": True,
                "default_tools": [],
                "agents": {"bot": {"allowed_tools": ["other_tool"]}},
            },
        )
        client = TestClient(create_app(initial_config=config))
        resp = client.post("/tool/send_email", json={}, headers={"X-Agent-Id": "bot"})
        # Would be 403 in enforce mode; shadowed -> 200 with would_block.
        assert resp.status_code == 200
        assert resp.json()["would_block"] is True


class TestShadowSchemaMock:
    def test_schema_shaped_response(self, httpserver):
        httpserver.expect_request("/run", method="POST").respond_with_json({"real": True})
        config = SidecarConfig(
            sidecar_id="sc", mode="shadow",
            tools=[ToolDefinition(
                name="lookup",
                endpoint=httpserver.url_for("/run"),
                schema={"type": "object", "properties": {
                    "rows": {"type": "array", "items": {"type": "object", "properties": {
                        "id": {"type": "integer"}, "name": {"type": "string"}}}},
                    "count": {"type": "integer"},
                }},
            )],
            policy=PolicyConfig(allow=["lookup"]),
        )
        client = TestClient(create_app(initial_config=config))
        resp = client.post("/tool/lookup", json={"q": "x"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["shadow"] is True
        syn = body["result"]["synthesized"]
        # Shaped to the schema: rows is a list of {id,name}; count is a number.
        assert "rows" in syn and isinstance(syn["rows"], list)
        assert syn["rows"][0].keys() == {"id", "name"}
        assert "count" in syn
        # Real backend never hit.
        assert len(httpserver.log) == 0

    def test_no_schema_falls_back_to_marker(self, httpserver):
        httpserver.expect_request("/run", method="POST").respond_with_json({"real": True})
        config = SidecarConfig(
            sidecar_id="sc", mode="shadow",
            tools=[ToolDefinition(name="plain", endpoint=httpserver.url_for("/run"))],
            policy=PolicyConfig(allow=["plain"]),
        )
        client = TestClient(create_app(initial_config=config))
        body = client.post("/tool/plain", json={}).json()
        assert body["result"]["note"].startswith("shadowed")
