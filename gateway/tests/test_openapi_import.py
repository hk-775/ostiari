"""Tests for OpenAPI import: parser, proxy param placement, and the gateway endpoint."""

from __future__ import annotations

import pytest
from ostiari_gateway.models import ModulesConfig, SidecarConfig, ToolDefinition
from ostiari_gateway.openapi_import import OpenAPIError, generate_tools
from ostiari_gateway.tool_proxy import ToolProxy
from starlette.testclient import TestClient

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/orders/{id}": {
            "parameters": [
                {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
            ],
            "get": {
                "operationId": "getOrder",
                "summary": "Fetch an order",
                "parameters": [{"name": "expand", "in": "query", "schema": {"type": "string"}}],
            },
            "delete": {"summary": "Delete an order"},
        },
        "/orders": {
            "post": {
                "operationId": "createOrder",
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "required": ["item"],
                    "properties": {"item": {"type": "string"}, "qty": {"type": "integer"}},
                }}}},
            },
        },
    },
}


class TestParser:
    def test_generates_one_tool_per_operation(self):
        tools = generate_tools(SPEC)
        names = {g.tool.name for g in tools}
        assert "getOrder" in names and "createOrder" in names
        # the delete op has no operationId -> derived name
        assert any(g.method == "DELETE" for g in tools)
        assert len(tools) == 3

    def test_path_and_query_params_classified(self):
        tools = {g.tool.name: g for g in generate_tools(SPEC)}
        get_order = tools["getOrder"].tool
        assert get_order.path_params == ["id"]
        assert get_order.query_params == ["expand"]
        assert get_order.endpoint == "https://api.example.com/v1/orders/{id}"

    def test_path_level_params_inherited_by_operations(self):
        # 'id' is declared at the path level; the DELETE op (no own params) must inherit it
        tools = {g.method: g for g in generate_tools(SPEC) if g.path == "/orders/{id}"}
        assert "id" in tools["DELETE"].tool.path_params

    def test_body_schema_becomes_properties(self):
        tools = {g.tool.name: g for g in generate_tools(SPEC)}
        create = tools["createOrder"].tool
        assert create.method == "POST"
        assert not create.path_params and not create.query_params
        props = create.schema_["properties"]
        assert "item" in props and "qty" in props
        assert create.schema_["required"] == ["item"]

    def test_name_prefix(self):
        tools = generate_tools(SPEC, name_prefix="crm.")
        assert all(g.tool.name.startswith("crm.") for g in tools)

    def test_server_url_override(self):
        tools = {g.tool.name: g for g in generate_tools(SPEC, server_url="http://local/api")}
        assert tools["createOrder"].tool.endpoint == "http://local/api/orders"

    def test_ref_resolution(self):
        spec = {
            "openapi": "3.0.0",
            "servers": [{"url": "http://x"}],
            "components": {"schemas": {"Pet": {
                "type": "object", "required": ["name"],
                "properties": {"name": {"type": "string"}}}}},
            "paths": {"/pets": {"post": {"operationId": "addPet", "requestBody": {
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}}}},
        }
        create = generate_tools(spec)[0].tool
        assert "name" in create.schema_["properties"]
        assert create.schema_["required"] == ["name"]

    def test_yaml_spec(self):
        yaml_spec = """
openapi: 3.0.0
servers: [{url: 'http://y'}]
paths:
  /ping:
    get:
      operationId: ping
"""
        tools = generate_tools(yaml_spec)
        assert tools[0].tool.name == "ping"

    def test_empty_or_invalid_raises(self):
        with pytest.raises(OpenAPIError):
            generate_tools({"openapi": "3.0.0", "paths": {}})
        with pytest.raises(OpenAPIError):
            generate_tools("not a spec at all: [")


class TestParamPlacement:
    def test_path_substitution_and_encoding(self):
        tool = ToolDefinition(name="t", endpoint="https://h/orders/{id}", method="GET",
                              path_params=["id"], query_params=["expand"])
        url, query, body = ToolProxy._place_params(tool, {"id": "A 1/2", "expand": "lines"})
        assert url == "https://h/orders/A%201%2F2"
        assert query == {"expand": "lines"}
        assert body is None

    def test_body_only_for_plain_tools(self):
        # hand-registered tool with no path/query params -> everything is body (unchanged behavior)
        tool = ToolDefinition(name="t", endpoint="https://h/send", method="POST")
        url, query, body = ToolProxy._place_params(tool, {"to": "x", "msg": "hi"})
        assert url == "https://h/send"
        assert query == {}
        assert body == {"to": "x", "msg": "hi"}

    def test_mixed_placement(self):
        tool = ToolDefinition(name="t", endpoint="https://h/u/{uid}/posts", method="POST",
                              path_params=["uid"], query_params=["draft"])
        url, query, body = ToolProxy._place_params(tool, {"uid": "7", "draft": "true", "title": "hi"})
        assert url == "https://h/u/7/posts"
        assert query == {"draft": "true"}
        assert body == {"title": "hi"}


class TestGatewayEndpoint:
    def _client(self) -> TestClient:
        from ostiari_gateway.server import create_app
        return TestClient(create_app(initial_config=SidecarConfig(
            sidecar_id="import-test", modules=ModulesConfig())))

    def test_preview_does_not_register(self):
        c = self._client()
        r = c.post("/config/tools/import-openapi", json={"spec": SPEC, "preview": True})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "preview" and data["count"] == 3
        # nothing registered
        assert c.get("/tools").json()["tools"] == []

    def test_import_registers_tools(self):
        c = self._client()
        r = c.post("/config/tools/import-openapi", json={"spec": SPEC})
        assert r.status_code == 200
        assert r.json()["status"] == "imported"
        registered = {t["name"] for t in c.get("/tools").json()["tools"]}
        assert "getOrder" in registered and "createOrder" in registered

    def test_replace_flag_clears_existing(self):
        c = self._client()
        c.post("/config/tools/keeper", json={"endpoint": "http://h/x"})  # add one first
        c.post("/config/tools/import-openapi", json={"spec": SPEC, "replace": True})
        registered = {t["name"] for t in c.get("/tools").json()["tools"]}
        assert "keeper" not in registered
        assert "getOrder" in registered

    def test_bad_spec_is_400(self):
        c = self._client()
        r = c.post("/config/tools/import-openapi", json={"spec": {"openapi": "3.0.0", "paths": {}}})
        assert r.status_code == 400

    def test_missing_source_is_400(self):
        c = self._client()
        r = c.post("/config/tools/import-openapi", json={})
        assert r.status_code == 400
