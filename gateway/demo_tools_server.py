"""Demo Tools Server — canned backends for the sandbox demo tools.

The seeded gateway tools (web_search, db_query, send_email, github.*) need a
real endpoint to proxy to. In the demo there are no live backends, so this
server returns realistic canned responses. It lets the Sandbox chat's agentic
loop actually call tools and get useful data back (e.g. "list my github repos").

Run: python demo_tools_server.py   (listens on http://localhost:9300)

Register the tools against a gateway so they proxy here — see
register_demo_tools.py, which the Makefile runs automatically.
"""

import json
import os
import posixpath
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="Ostiari Demo Tools")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


_REPOS = [
    {"name": "ostiari", "visibility": "public", "language": "Python", "stars": 1284, "updated": "2026-07-14"},
    {"name": "axon-llm", "visibility": "public", "language": "Python", "stars": 892, "updated": "2026-07-11"},
    {"name": "trust-safety", "visibility": "private", "language": "TypeScript", "stars": 0, "updated": "2026-07-15"},
    {"name": "agent-sandbox", "visibility": "private", "language": "Python", "stars": 3, "updated": "2026-07-09"},
    {"name": "infra-terraform", "visibility": "private", "language": "HCL", "stars": 1, "updated": "2026-06-30"},
]

_FILES = {
    "/README.md": (
        "# Ostiari MCP sandbox\n\n"
        "This in-memory filesystem is served by the deployed demo MCP endpoint.\n"
    ),
    "/reports/weekly.txt": "Gateway fleet healthy: 4/4\nMCP servers connected: 8/8\n",
}


@app.post("/web_search")
async def web_search(body: dict) -> dict:
    query = body.get("query", "")
    return {
        "query": query,
        "results": [
            {"title": f"Result for '{query}' — Ostiari docs", "url": "https://ostiari.dev/docs", "snippet": "Runtime safety and reliability layer for AI agents."},
            {"title": f"'{query}' on GitHub", "url": "https://github.com/search", "snippet": "Repositories, code, and issues matching your query."},
            {"title": f"Wikipedia: {query}", "url": "https://en.wikipedia.org/wiki", "snippet": "Overview and background."},
        ],
    }


@app.post("/db_query")
async def db_query(body: dict) -> dict:
    sql = body.get("sql", "")
    return {
        "sql": sql,
        "rows": [
            {"id": 1, "name": "Ada Lovelace", "email": "ada@example.com", "plan": "enterprise"},
            {"id": 2, "name": "Alan Turing", "email": "alan@example.com", "plan": "pro"},
            {"id": 3, "name": "Grace Hopper", "email": "grace@example.com", "plan": "pro"},
        ],
        "row_count": 3,
    }


@app.post("/send_email")
async def send_email(body: dict) -> dict:
    return {
        "status": "sent",
        "to": body.get("to", ""),
        "subject": body.get("subject", ""),
        "message_id": "demo-msg-8f3a21",
    }


@app.post("/github.list_repos")
async def github_list_repos(body: dict) -> dict:
    return {"repos": _REPOS, "total": len(_REPOS)}


@app.post("/github.search_code")
async def github_search_code(body: dict) -> dict:
    query = body.get("query", "")
    return {
        "query": query,
        "matches": [
            {"repo": "ostiari", "path": "gateway/ostiari_gateway/server.py", "line": 137, "preview": f"# match for '{query}'"},
            {"repo": "axon-llm", "path": "src/router.py", "line": 42, "preview": f"def route(): # {query}"},
        ],
        "total": 2,
    }


@app.post("/github.create_issue")
async def github_create_issue(body: dict) -> dict:
    return {
        "status": "created",
        "repo": body.get("repo", "ostiari"),
        "number": 231,
        "title": body.get("title", ""),
        "url": "https://github.com/acme/ostiari/issues/231",
    }


@app.post("/drawio.list_diagrams")
async def drawio_list_diagrams(body: dict) -> dict:
    return {"diagrams": [
        {"id": "d1", "name": "System Architecture", "shapes": 12},
        {"id": "d2", "name": "Data Flow", "shapes": 7},
    ], "total": 2}


@app.post("/drawio.create_diagram")
async def drawio_create_diagram(body: dict) -> dict:
    return {"status": "created", "id": "d3", "name": body.get("name", "Untitled"), "url": "https://app.diagrams.net/#d3"}


@app.post("/drawio.add_shape")
async def drawio_add_shape(body: dict) -> dict:
    return {"status": "added", "diagram_id": body.get("diagram_id", "d1"),
            "shape": body.get("shape", "rectangle"), "label": body.get("label", "")}


# Destructive tools — registered so scenarios exercise the policy guard.
# In practice the gateway policy blocks these before the request reaches here;
# the handlers exist only as a fallback if policy is disabled.
@app.post("/db_delete")
async def db_delete(body: dict) -> dict:
    return {"status": "deleted", "table": body.get("table", "")}


@app.post("/github.delete_repo")
async def github_delete_repo(body: dict) -> dict:
    return {"status": "deleted", "repo": body.get("repo", "")}


@app.post("/drawio.delete_diagram")
async def drawio_delete_diagram(body: dict) -> dict:
    return {"status": "deleted", "id": body.get("id", "")}


# ─── Paywalled tool (native x402 passthrough demo) ──────────────────────────
# Returns HTTP 402 Payment Required unless the request carries an X-PAYMENT
# header. Ostiari's payment gate (passthrough mode) settles the charge from the
# agent's wallet and retries with that header, so a funded agent gets results
# and an unfunded one is blocked at 402 — all without touching a blockchain.

PREMIUM_SEARCH_PRICE_USDC = 0.005


@app.post("/premium_search")
async def premium_search(request: Request) -> JSONResponse:
    if not request.headers.get("X-PAYMENT"):
        # x402 challenge: tell the caller what to pay.
        return JSONResponse(
            status_code=402,
            content={
                "error": "Payment required",
                "amount_usdc": PREMIUM_SEARCH_PRICE_USDC,
                "asset": "USDC",
                "pay_to": "0xDemoMerchantWalletF0rPremiumSearch",
                "nonce": "premium-search",
            },
        )
    body = await request.json()
    query = body.get("query", "")
    return JSONResponse(content={
        "query": query,
        "paid_usdc": PREMIUM_SEARCH_PRICE_USDC,
        "results": [
            {"title": f"[Premium] Deep report on '{query}'", "url": "https://premium.example/report",
             "snippet": "Full-text analysis, citations, and competitive breakdown."},
            {"title": f"[Premium] Dataset: {query}", "url": "https://premium.example/data",
             "snippet": "Structured, licensed dataset behind the paywall."},
        ],
    })


# ─── Remote MCP servers ────────────────────────────────────────────────────

_MCP_TOOLS: dict[str, list[dict[str, Any]]] = {
    "drawio": [
        {
            "name": "list_diagrams",
            "description": "List available diagrams.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "create_diagram",
            "description": "Create a diagram.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "add_shape",
            "description": "Add a shape to an existing diagram.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "diagram_id": {"type": "string"},
                    "shape": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["diagram_id", "shape"],
            },
        },
        {
            "name": "delete_diagram",
            "description": "Delete a diagram.",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    ],
    "filesystem": [
        {
            "name": "list_directory",
            "description": "List files in the demo MCP sandbox.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
        {
            "name": "read_text_file",
            "description": "Read a UTF-8 text file from the demo MCP sandbox.",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "Write a UTF-8 text file in the demo MCP sandbox.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    ],
}


def _mcp_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _mcp_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _sandbox_path(value: str) -> str:
    raw = value.strip() or "/"
    if ".." in raw.split("/"):
        raise ValueError("path traversal is not allowed")
    normalized = posixpath.normpath("/" + raw.lstrip("/"))
    return normalized if normalized.startswith("/") else f"/{normalized}"


def _filesystem_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    path = _sandbox_path(str(arguments.get("path", "/")))
    if name == "list_directory":
        prefix = "/" if path == "/" else f"{path.rstrip('/')}/"
        entries: dict[str, str] = {}
        for filename in _FILES:
            if not filename.startswith(prefix):
                continue
            remainder = filename[len(prefix):]
            if not remainder:
                continue
            first, separator, _rest = remainder.partition("/")
            entries[first] = "directory" if separator else "file"
        return {
            "path": path,
            "entries": [
                {"name": entry, "type": entry_type}
                for entry, entry_type in sorted(entries.items())
            ],
        }
    if name == "read_text_file":
        if path not in _FILES:
            raise ValueError(f"file not found: {path}")
        return {"path": path, "content": _FILES[path]}
    if name == "write_file":
        content = str(arguments.get("content", ""))
        _FILES[path] = content
        return {"path": path, "bytes": len(content.encode()), "status": "written"}
    raise ValueError(f"unknown filesystem tool: {name}")


async def _mcp_tool_call(
    server: str,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if server == "drawio":
        handlers = {
            "list_diagrams": drawio_list_diagrams,
            "create_diagram": drawio_create_diagram,
            "add_shape": drawio_add_shape,
            "delete_diagram": drawio_delete_diagram,
        }
        handler = handlers.get(name)
        if handler is None:
            raise ValueError(f"unknown draw.io tool: {name}")
        return await handler(arguments)
    if server == "filesystem":
        return _filesystem_call(name, arguments)
    raise ValueError(f"unknown MCP server: {server}")


@app.post("/mcp/{server}")
async def remote_mcp(server: str, request: Request) -> Response:
    """Minimal Streamable-HTTP MCP endpoint used by the deployed gateways."""
    if server not in _MCP_TOOLS:
        return JSONResponse(
            status_code=404,
            content=_mcp_error(None, -32601, f"unknown MCP server: {server}"),
        )
    message = await request.json()
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        return JSONResponse(content=_mcp_result(request_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": f"ostiari-demo-{server}",
                "version": "1.0.0",
            },
        }))
    if method == "notifications/initialized" or request_id is None:
        return Response(status_code=202)
    if method == "tools/list":
        return JSONResponse(content=_mcp_result(
            request_id,
            {"tools": _MCP_TOOLS[server]},
        ))
    if method == "tools/call":
        name = str(params.get("name", ""))
        if not name:
            return JSONResponse(content=_mcp_error(
                request_id,
                -32602,
                "missing tool name",
            ))
        try:
            result = await _mcp_tool_call(
                server,
                name,
                params.get("arguments") or {},
            )
        except ValueError as exc:
            return JSONResponse(content=_mcp_error(
                request_id,
                -32000,
                str(exc),
            ))
        return JSONResponse(content=_mcp_result(request_id, {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, sort_keys=True),
                }
            ],
            "isError": False,
        }))
    if method == "ping":
        return JSONResponse(content=_mcp_result(request_id, {}))
    return JSONResponse(content=_mcp_error(
        request_id,
        -32601,
        f"method not found: {method}",
    ))


# ─── A2A demo agent ────────────────────────────────────────────────────────

_A2A_TASKS: dict[str, dict[str, Any]] = {}


def _a2a_base_url() -> str:
    return os.environ.get(
        "OSTIARI_DEMO_A2A_BASE_URL",
        "http://localhost:9300",
    ).rstrip("/")


@app.get("/.well-known/agent.json")
async def a2a_agent_card() -> dict[str, Any]:
    return {
        "name": "DevOps Assistant",
        "description": (
            "Handles CI/CD operations, deployments, and infrastructure tasks"
        ),
        "url": f"{_a2a_base_url()}/a2a",
        "version": "1.0.0",
        "capabilities": {
            "streaming": False,
            "push_notifications": False,
        },
        "skills": [
            {
                "id": "deploy",
                "name": "Deploy Service",
                "description": "Deploy a service to staging or production.",
            },
            {
                "id": "rollback",
                "name": "Rollback Deployment",
                "description": "Rollback a service to its previous version.",
            },
            {
                "id": "status",
                "name": "Check Status",
                "description": "Check deployment status and service health.",
            },
        ],
    }


def _a2a_response(message: str) -> str:
    text = message.lower()
    if "rollback" in text:
        return (
            "Rollback completed for auth-service: v2.4.1 → v2.4.0. "
            "All health checks are passing."
        )
    if "status" in text:
        return (
            "auth-service is healthy: version v2.4.0, 3/3 replicas, "
            "0.02% error rate."
        )
    if "deploy" in text:
        return (
            "Deployment completed for auth-service in staging. "
            "Version v2.4.1 is healthy on 3/3 replicas."
        )
    return "I can deploy, rollback, or report service status."


@app.post("/a2a")
async def a2a_task(request: Request) -> JSONResponse:
    body = await request.json()
    request_id = body.get("id")
    if body.get("jsonrpc") != "2.0":
        return JSONResponse(content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32600, "message": "Invalid request"},
        })

    method = body.get("method")
    params = body.get("params") or {}
    if method == "tasks/send":
        message = params.get("message") or {}
        text = next(
            (
                str(part.get("text", ""))
                for part in message.get("parts", [])
                if part.get("type") == "text"
            ),
            "",
        )
        task_id = str(params.get("id") or f"task-{uuid.uuid4().hex[:8]}")
        task = {
            "id": task_id,
            "status": {"state": "completed"},
            "history": [
                {"role": "user", "parts": [{"type": "text", "text": text}]},
                {
                    "role": "agent",
                    "parts": [{"type": "text", "text": _a2a_response(text)}],
                },
            ],
            "artifacts": [],
        }
        _A2A_TASKS[task_id] = task
        return JSONResponse(content={
            "jsonrpc": "2.0",
            "id": request_id,
            "result": task,
        })
    if method == "tasks/get":
        task_id = str(params.get("id", ""))
        task = _A2A_TASKS.get(task_id)
        if task is not None:
            return JSONResponse(content={
                "jsonrpc": "2.0",
                "id": request_id,
                "result": task,
            })
        return JSONResponse(content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32001, "message": f"Task not found: {task_id}"},
        })
    return JSONResponse(content={
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    })


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "service": "demo-tools",
        "mcp_servers": sorted(_MCP_TOOLS),
        "a2a_agent": "DevOps Assistant",
    }


if __name__ == "__main__":
    print("\n  Ostiari Demo Tools server at http://localhost:9300")
    print("  Serves: web_search, db_query, send_email, github.list_repos, github.search_code, github.create_issue\n")
    uvicorn.run(app, host="0.0.0.0", port=9300)
