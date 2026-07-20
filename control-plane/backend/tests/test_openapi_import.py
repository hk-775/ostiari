"""Tests for the control-plane OpenAPI import endpoint (DB-backed)."""

import pytest

pytestmark = pytest.mark.anyio

SPEC = {
    "openapi": "3.0.0",
    "servers": [{"url": "https://api.example.com/v1"}],
    "paths": {
        "/orders/{id}": {
            "get": {
                "operationId": "getOrder",
                "summary": "Fetch an order",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                    {"name": "expand", "in": "query", "schema": {"type": "string"}},
                ],
            },
        },
        "/orders": {
            "post": {
                "operationId": "createOrder",
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object", "required": ["item"],
                    "properties": {"item": {"type": "string"}},
                }}}},
            },
        },
    },
}


async def _make_gateway(client, gid="gw1"):
    return await client.post("/api/gateways", json={
        "id": gid, "name": f"GW {gid}", "endpoint": "http://localhost:9001", "description": "d",
    })


class TestOpenAPIImport:
    async def test_preview_does_not_persist(self, client):
        await _make_gateway(client)
        r = await client.post("/api/tools/gw1/import-openapi", json={"spec": SPEC, "preview": True})
        assert r.status_code == 200
        assert r.json()["status"] == "preview" and r.json()["count"] == 2
        # nothing persisted
        assert (await client.get("/api/tools?gateway_id=gw1")).json() == []

    async def test_import_persists_with_param_placement(self, client):
        await _make_gateway(client)
        r = await client.post("/api/tools/gw1/import-openapi", json={"spec": SPEC})
        assert r.status_code == 200 and r.json()["status"] == "imported"

        tools = (await client.get("/api/tools?gateway_id=gw1")).json()
        by_name = {t["name"]: t for t in tools}
        assert "getOrder" in by_name and "createOrder" in by_name
        assert by_name["getOrder"]["path_params"] == ["id"]
        assert by_name["getOrder"]["query_params"] == ["expand"]

    async def test_reimport_upserts_not_duplicates(self, client):
        await _make_gateway(client)
        await client.post("/api/tools/gw1/import-openapi", json={"spec": SPEC})
        await client.post("/api/tools/gw1/import-openapi", json={"spec": SPEC})
        tools = (await client.get("/api/tools?gateway_id=gw1")).json()
        names = [t["name"] for t in tools]
        assert names.count("getOrder") == 1  # upserted, not duplicated

    async def test_replace_clears_prior_tools(self, client):
        await _make_gateway(client)
        await client.post("/api/tools/gw1", json={"name": "keeper", "endpoint": "http://h/x"})
        await client.post("/api/tools/gw1/import-openapi", json={"spec": SPEC, "replace": True})
        names = {t["name"] for t in (await client.get("/api/tools?gateway_id=gw1")).json()}
        assert "keeper" not in names and "getOrder" in names

    async def test_unknown_gateway_404(self, client):
        r = await client.post("/api/tools/nope/import-openapi", json={"spec": SPEC})
        assert r.status_code == 404

    async def test_bad_spec_400(self, client):
        await _make_gateway(client)
        r = await client.post("/api/tools/gw1/import-openapi",
                             json={"spec": {"openapi": "3.0.0", "paths": {}}})
        assert r.status_code == 400
