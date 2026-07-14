"""A2A Demo Server — a simple agent that responds to tasks.

Run: python a2a_demo_server.py
Exposes:
  GET  /.well-known/agent.json  — AgentCard (discovery)
  POST /a2a                     — JSON-RPC task handling

Use from the Control Plane Sandbox A2A tab:
  1. Enter http://localhost:9200 as the agent URL
  2. Click Discover — loads the AgentCard
  3. Send a task message
"""

import json
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

app = FastAPI(title="A2A Demo Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

AGENT_CARD = {
    "name": "DevOps Assistant",
    "description": "Handles CI/CD operations, deployments, and infrastructure tasks",
    "url": "http://localhost:9200/a2a",
    "version": "1.0.0",
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
    },
    "skills": [
        {
            "id": "deploy",
            "name": "Deploy Service",
            "description": "Deploy a service to a target environment (staging, production)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": "Service name"},
                    "environment": {"type": "string", "enum": ["staging", "production"]},
                },
                "required": ["service", "environment"],
            },
        },
        {
            "id": "rollback",
            "name": "Rollback Deployment",
            "description": "Rollback a service to the previous version",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "version": {"type": "string", "description": "Target version to rollback to"},
                },
                "required": ["service"],
            },
        },
        {
            "id": "status",
            "name": "Check Status",
            "description": "Check deployment status and health of a service",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                },
                "required": ["service"],
            },
        },
    ],
}

tasks: dict[str, dict] = {}


def handle_task(message_text: str) -> dict:
    """Simulate processing a task and return a response."""
    text = message_text.lower()

    if "deploy" in text:
        return {
            "state": "completed",
            "response": "Deployment initiated successfully.\n\n"
                        "Service: auth-service\n"
                        "Environment: staging\n"
                        "Version: v2.4.1\n"
                        "Status: HEALTHY\n"
                        "Pods: 3/3 running\n"
                        "Duration: 45s\n\n"
                        "All health checks passing. Ready for production promotion.",
            "artifacts": [{"name": "deployment-log", "data": {"version": "v2.4.1", "pods": 3, "healthy": True}}],
        }
    elif "rollback" in text:
        return {
            "state": "completed",
            "response": "Rollback completed.\n\n"
                        "Service: auth-service\n"
                        "Rolled back from: v2.4.1 → v2.4.0\n"
                        "Status: HEALTHY\n"
                        "Duration: 12s\n\n"
                        "Previous version restored. All health checks passing.",
            "artifacts": [{"name": "rollback-log", "data": {"from": "v2.4.1", "to": "v2.4.0"}}],
        }
    elif "status" in text:
        return {
            "state": "completed",
            "response": "Service Status Report:\n\n"
                        "auth-service:\n"
                        "  Version: v2.4.0\n"
                        "  Replicas: 3/3 healthy\n"
                        "  CPU: 23% avg\n"
                        "  Memory: 412MB / 1024MB\n"
                        "  Uptime: 4d 7h\n"
                        "  Last deploy: 2h ago\n"
                        "  Error rate: 0.02%\n\n"
                        "All systems nominal.",
            "artifacts": [],
        }
    else:
        return {
            "state": "completed",
            "response": f"I can help with deployments, rollbacks, and status checks.\n\n"
                        f"Try asking:\n"
                        f"- 'Deploy auth-service to staging'\n"
                        f"- 'Rollback auth-service'\n"
                        f"- 'Check status of auth-service'\n\n"
                        f"You said: \"{message_text}\"",
            "artifacts": [],
        }


@app.get("/.well-known/agent.json")
async def agent_card():
    return AGENT_CARD


@app.post("/a2a")
async def handle_a2a(request: Request):
    body = await request.json()

    if body.get("jsonrpc") != "2.0":
        return JSONResponse({"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request"}, "id": body.get("id")})

    method = body.get("method")
    params = body.get("params", {})
    req_id = body.get("id", str(uuid.uuid4()))

    if method == "tasks/send":
        message = params.get("message", {})
        parts = message.get("parts", [])
        text = ""
        for part in parts:
            if part.get("type") == "text":
                text = part.get("text", "")
                break

        task_id = f"task-{uuid.uuid4().hex[:8]}"
        result = handle_task(text)

        task = {
            "id": task_id,
            "status": {"state": result["state"]},
            "history": [
                {"role": "user", "parts": [{"type": "text", "text": text}]},
                {"role": "agent", "parts": [{"type": "text", "text": result["response"]}]},
            ],
            "artifacts": [
                {"name": a["name"], "parts": [{"type": "data", "data": json.dumps(a["data"])}]}
                for a in result.get("artifacts", [])
            ],
        }
        tasks[task_id] = task

        return {"jsonrpc": "2.0", "result": task, "id": req_id}

    elif method == "tasks/get":
        task_id = params.get("id")
        if task_id in tasks:
            return {"jsonrpc": "2.0", "result": tasks[task_id], "id": req_id}
        return {"jsonrpc": "2.0", "error": {"code": -32001, "message": f"Task not found: {task_id}"}, "id": req_id}

    elif method == "tasks/cancel":
        task_id = params.get("id")
        if task_id in tasks:
            tasks[task_id]["status"]["state"] = "canceled"
            return {"jsonrpc": "2.0", "result": tasks[task_id], "id": req_id}
        return {"jsonrpc": "2.0", "error": {"code": -32001, "message": f"Task not found: {task_id}"}, "id": req_id}

    else:
        return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Method not found: {method}"}, "id": req_id}


if __name__ == "__main__":
    print("\n  A2A Demo Agent running at http://localhost:9200")
    print("  AgentCard: http://localhost:9200/.well-known/agent.json")
    print("  Endpoint:  http://localhost:9200/a2a")
    print("\n  Skills: deploy, rollback, status\n")
    uvicorn.run(app, host="0.0.0.0", port=9200)
