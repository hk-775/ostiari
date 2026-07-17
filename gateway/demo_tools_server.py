"""Demo Tools Server — canned backends for the sandbox demo tools.

The seeded gateway tools (web_search, db_query, send_email, github.*) need a
real endpoint to proxy to. In the demo there are no live backends, so this
server returns realistic canned responses. It lets the Sandbox chat's agentic
loop actually call tools and get useful data back (e.g. "list my github repos").

Run: python demo_tools_server.py   (listens on http://localhost:9300)

Register the tools against a gateway so they proxy here — see
register_demo_tools.py, which the Makefile runs automatically.
"""

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI(title="Ostiari Demo Tools")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


_REPOS = [
    {"name": "ostiari", "visibility": "public", "language": "Python", "stars": 1284, "updated": "2026-07-14"},
    {"name": "axon-llm", "visibility": "public", "language": "Python", "stars": 892, "updated": "2026-07-11"},
    {"name": "trust-safety", "visibility": "private", "language": "TypeScript", "stars": 0, "updated": "2026-07-15"},
    {"name": "agent-sandbox", "visibility": "private", "language": "Python", "stars": 3, "updated": "2026-07-09"},
    {"name": "infra-terraform", "visibility": "private", "language": "HCL", "stars": 1, "updated": "2026-06-30"},
]


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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "demo-tools"}


if __name__ == "__main__":
    print("\n  Ostiari Demo Tools server at http://localhost:9300")
    print("  Serves: web_search, db_query, send_email, github.list_repos, github.search_code, github.create_issue\n")
    uvicorn.run(app, host="0.0.0.0", port=9300)
