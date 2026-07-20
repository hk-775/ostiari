# Import tools from OpenAPI (and expose them over MCP)

Most software an agent needs to touch already speaks REST and ships an OpenAPI
(Swagger) description. Ostiari can turn that spec into governed tools in one
step — every generated tool inherits the full gate chain (parameter-aware risk,
quota, human-in-the-loop, anomaly, dynamic trust, tracing, decision
explainability). Optionally, the same tools can be re-exposed to external MCP
clients through a governed bridge.

```
OpenAPI spec ──▶ generate tools ──▶ register on a gateway ──▶ governed on every call
                                          │
                                          └──▶ (optional) MCP bridge ──▶ external MCP clients
```

## Why

Writing tool definitions by hand for hundreds of endpoints is tedious and drifts
out of sync with the API. Generation makes an entire API surface agent-callable
at once, and — unlike a plain API gateway that only governs the *request* —
every generated call passes through Ostiari's *behavioral* governance.

## What gets generated

One tool per OpenAPI operation (path × method):

| OpenAPI | Ostiari tool |
|---|---|
| `POST /orders {item, qty}` | `createOrder(item, qty)` — body params |
| `GET /orders/{id}?expand=` | `getOrder(id, expand)` — `id` path, `expand` query |
| `DELETE /orders/{id}` | `delete_orders_id(id)` — derived name, `id` path |

The generator resolves local `$ref`s, inherits path-level parameters, derives a
stable tool name from `operationId` (falling back to `method_path`), and records
**parameter placement** (`path_params` / `query_params`) so the proxy rebuilds
the real HTTP call — substituting path vars into the URL, splitting query vs.
JSON body — from the single flat argument dict the model provides. Tools with no
path/query params behave exactly as hand-registered tools (everything is body).

## Import it

**CLI** (targets a running gateway):

```bash
# Preview without registering
ostiari import-openapi https://api.example.com/openapi.json --gateway http://localhost:8421 --preview

# Import (URL, local file, or literal JSON/YAML), optionally namespaced
ostiari import-openapi ./petstore.yaml --gateway http://localhost:8421 --name-prefix "pets."

# Replace all of a gateway's tools instead of merging
ostiari import-openapi ./spec.json --gateway http://localhost:8421 --replace
```

**Control plane UI:** Tools page → **Import from OpenAPI** → pick a gateway,
paste a URL or spec, **Preview** the generated tools, then **Import**. Imported
tools are persisted (with their param placement) and pushed to the gateway like
any other config, so they survive restarts.

**HTTP:**

- Gateway (register directly, no persistence): `POST /config/tools/import-openapi`
  with `{"source": <url|json|yaml>}` or `{"spec": <object>}`, plus optional
  `server_url`, `name_prefix`, `replace`, `preview`.
- Control plane (persist + push): `POST /api/tools/{gateway_id}/import-openapi`
  with the same body. Re-importing **upserts** by name (no duplicates).

## Expose the tools over MCP (optional bridge)

To make the imported tools reachable by *external* MCP clients (Claude Desktop,
IDEs, other agents) without giving up governance, run the bridge — it speaks the
MCP JSON-RPC protocol and forwards every `tools/call` back through the gateway's
`POST /tool/{name}`, so the gate chain still applies. A governance block surfaces
to the MCP client as a tool error (`isError`) with the reason, not a crash.

```bash
python -m ostiari_gateway.mcp_bridge --gateway http://localhost:8421 --port 8600
# MCP endpoint: http://localhost:8600/mcp
```

The bridge implements `initialize`, `tools/list`, and `tools/call`, and needs no
MCP SDK dependency.

## Notes / limitations

- OpenAPI 3.x. JSON and YAML both accepted.
- Non-object request bodies (array/primitive) are exposed as a single `body`
  field.
- The control-plane `tools` table gained nullable `path_params` / `query_params`
  columns; the schema is created via `create_all` (no migrations), so a
  pre-existing database file needs those columns added or a fresh DB.
- The generator inspects only JSON media types for request bodies.
