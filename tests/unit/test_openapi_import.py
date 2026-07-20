"""Unit tests for the shared OpenAPI spec parser (ostiari.openapi_import)."""

from __future__ import annotations

import pytest

from ostiari.openapi_import import OpenAPIError, is_url, parse_spec

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


def _by_name(specs):
    return {s["name"]: s for s in specs}


def test_one_tool_per_operation():
    specs = parse_spec(SPEC)
    names = {s["name"] for s in specs}
    assert "getOrder" in names and "createOrder" in names
    assert len(specs) == 3


def test_param_placement():
    s = _by_name(parse_spec(SPEC))["getOrder"]
    assert s["path_params"] == ["id"]
    assert s["query_params"] == ["expand"]
    assert s["endpoint"] == "https://api.example.com/v1/orders/{id}"


def test_path_level_params_inherited():
    delete = next(s for s in parse_spec(SPEC) if s["method"] == "DELETE")
    assert "id" in delete["path_params"]


def test_body_schema_to_properties():
    s = _by_name(parse_spec(SPEC))["createOrder"]
    assert not s["path_params"] and not s["query_params"]
    assert set(s["schema"]["properties"]) == {"item", "qty"}
    assert s["schema"]["required"] == ["item"]


def test_name_prefix_and_server_override():
    specs = _by_name(parse_spec(SPEC, name_prefix="crm.", server_url="http://local/api"))
    assert "crm.createOrder" in specs
    assert specs["crm.createOrder"]["endpoint"] == "http://local/api/orders"


def test_ref_resolution():
    spec = {
        "openapi": "3.0.0", "servers": [{"url": "http://x"}],
        "components": {"schemas": {"Pet": {
            "type": "object", "required": ["name"],
            "properties": {"name": {"type": "string"}}}}},
        "paths": {"/pets": {"post": {"operationId": "addPet", "requestBody": {
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}}}},
    }
    s = parse_spec(spec)[0]
    assert "name" in s["schema"]["properties"]
    assert s["schema"]["required"] == ["name"]


def test_yaml_spec():
    yaml_spec = "openapi: 3.0.0\nservers: [{url: 'http://y'}]\npaths:\n  /ping:\n    get:\n      operationId: ping\n"
    assert parse_spec(yaml_spec)[0]["name"] == "ping"


def test_derived_name_without_operation_id():
    delete = next(s for s in parse_spec(SPEC) if s["method"] == "DELETE")
    assert delete["name"]  # non-empty derived name
    assert "orders" in delete["name"]


def test_errors():
    with pytest.raises(OpenAPIError):
        parse_spec({"openapi": "3.0.0", "paths": {}})
    with pytest.raises(OpenAPIError):
        parse_spec("not valid: [")


def test_is_url():
    assert is_url("https://x.com/spec.json")
    assert not is_url("/local/file.yaml")
