"""Connect the demo's real MCP servers to the running crm-agent gateway.

The control plane's config push applies tools + policy but does NOT connect MCP
servers (they'd spawn subprocesses on every push). MCP servers connect at
gateway startup (from llm-gateway-config.yaml) or via POST /config/mcp-servers.
This script does the latter so `make dev` / `make demo-full` bring up the real
draw.io + filesystem MCP servers without restarting the gateway.

Idempotent: add_server reconnects if already present. The gateway spawns the
real stdio subprocess (npx) and discovers each server's real tools.

Run after the gateway is up:
    python register_demo_mcp.py
The Makefile runs this automatically as part of `make dev` / `make demo-full`.
"""

import json
import shutil
import sys
import time
import urllib.error
import urllib.request

GATEWAY = "http://localhost:8421"

# The scratch dir the filesystem MCP server is sandboxed to. Created here (with
# a sample file) so its tools return real data in the demo.
SANDBOX = "/tmp/ostiari-mcp-sandbox"

SERVERS = [
    {"name": "drawio", "prefix": "drawio", "npx_args": ["-y", "drawio-mcp-server"]},
    {"name": "filesystem", "prefix": "fs",
     "npx_args": ["-y", "@modelcontextprotocol/server-filesystem", SANDBOX]},
]


def _req(method: str, url: str, body: dict | None = None) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"detail": raw}


def _wait_for(url: str, label: str, tries: int = 30) -> bool:
    for _ in range(tries):
        try:
            status, _ = _req("GET", url)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    print(f"  ! {label} not reachable at {url} after {tries}s")
    return False


def main() -> int:
    npx = shutil.which("npx")
    if not npx:
        print("  ! npx not found on PATH — install Node.js to run the MCP demo. Skipping.")
        return 0

    print("Connecting real MCP servers to crm-agent")
    if not _wait_for(f"{GATEWAY}/health", "crm-agent gateway"):
        return 1

    # Sandbox dir + sample file so the filesystem MCP tools return real data.
    import os
    os.makedirs(SANDBOX, exist_ok=True)
    readme = os.path.join(SANDBOX, "README.txt")
    if not os.path.exists(readme):
        with open(readme, "w") as f:
            f.write("Ostiari demo sandbox — filesystem MCP server operates here.\n")

    for s in SERVERS:
        config = {
            "name": s["name"], "mode": "stdio", "prefix": s["prefix"],
            "command": [npx, *s["npx_args"]],
        }
        status, resp = _req("POST", f"{GATEWAY}/config/mcp-servers", config)
        if status == 200 and resp and resp.get("status") == "connected":
            print(f"  + {s['name']}: connected, {resp.get('tools_discovered', 0)} tools discovered")
        else:
            detail = (resp or {}).get("error") or (resp or {}).get("detail") or resp
            print(f"  ! {s['name']}: {status} {detail}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
