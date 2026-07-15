"""Point crm-agent's demo tools at the demo tools server and add GitHub tools.

Idempotent: deletes any existing crm-agent tools with these names, re-adds them
pointing at http://localhost:9300, then pushes the config to the live gateway so
the Sandbox chat can call them and get useful canned data back.

Run after the control plane and demo_tools_server.py are up:
    python register_demo_tools.py
The Makefile runs this automatically as part of `make demo-full`.
"""

import sys
import time
import urllib.error
import urllib.request

CONTROL_PLANE = "http://localhost:8400"
GATEWAY_ID = "crm-agent"
DEMO_TOOLS = "http://localhost:9300"

TOOLS = [
    {"name": "web_search", "endpoint": f"{DEMO_TOOLS}/web_search", "method": "POST",
     "description": "Search the web for information on a topic. Args: query (string)."},
    {"name": "db_query", "endpoint": f"{DEMO_TOOLS}/db_query", "method": "POST",
     "description": "Run a read-only SQL query against the app database. Args: sql (string)."},
    {"name": "send_email", "endpoint": f"{DEMO_TOOLS}/send_email", "method": "POST",
     "description": "Send an email. Args: to (string), subject (string), body (string)."},
    {"name": "github.list_repos", "endpoint": f"{DEMO_TOOLS}/github.list_repos", "method": "POST",
     "description": "List the user's GitHub repositories. No required args."},
    {"name": "github.search_code", "endpoint": f"{DEMO_TOOLS}/github.search_code", "method": "POST",
     "description": "Search code across GitHub repositories. Args: query (string)."},
    {"name": "github.create_issue", "endpoint": f"{DEMO_TOOLS}/github.create_issue", "method": "POST",
     "description": "Open a GitHub issue. Args: repo (string), title (string), body (string)."},
    {"name": "drawio.list_diagrams", "endpoint": f"{DEMO_TOOLS}/drawio.list_diagrams", "method": "POST",
     "description": "List draw.io diagrams. No required args."},
    {"name": "drawio.create_diagram", "endpoint": f"{DEMO_TOOLS}/drawio.create_diagram", "method": "POST",
     "description": "Create a draw.io diagram. Args: name (string)."},
    {"name": "drawio.add_shape", "endpoint": f"{DEMO_TOOLS}/drawio.add_shape", "method": "POST",
     "description": "Add a shape to a diagram. Args: diagram_id (string), shape (string), label (string)."},
    # Destructive tools — registered so the Scenarios demo exercises the policy
    # guard (the block-destructive policy blocks these before they run).
    {"name": "db_delete", "endpoint": f"{DEMO_TOOLS}/db_delete", "method": "POST",
     "description": "Delete rows from a database table. Args: table (string)."},
    {"name": "github.delete_repo", "endpoint": f"{DEMO_TOOLS}/github.delete_repo", "method": "POST",
     "description": "Delete a GitHub repository. Args: repo (string)."},
    {"name": "drawio.delete_diagram", "endpoint": f"{DEMO_TOOLS}/drawio.delete_diagram", "method": "POST",
     "description": "Delete a draw.io diagram. Args: id (string)."},
]

# Block patterns for the crm-agent demo. fnmatch-style; must match the actual
# action names the Scenarios tab calls (db_delete, github.delete_repo, ...).
BLOCK_PATTERNS = ["*delete*", "*.drop", "*.destroy", "db_delete"]


def _req(method: str, url: str, body: dict | None = None) -> tuple[int, dict | list | None]:
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        import json
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            import json
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        import json
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
    print("Registering demo tools on", GATEWAY_ID)
    if not _wait_for(f"{CONTROL_PLANE}/api/health", "control plane"):
        return 1
    if not _wait_for(f"{DEMO_TOOLS}/health", "demo tools server"):
        return 1

    names = {t["name"] for t in TOOLS}

    # Remove existing tools with these names (idempotent re-run)
    status, existing = _req("GET", f"{CONTROL_PLANE}/api/tools?gateway_id={GATEWAY_ID}")
    if status == 200 and isinstance(existing, list):
        for t in existing:
            if t["name"] in names:
                _req("DELETE", f"{CONTROL_PLANE}/api/tools/{t['id']}")

    # Add the demo-backed tools
    for t in TOOLS:
        status, resp = _req("POST", f"{CONTROL_PLANE}/api/tools/{GATEWAY_ID}", t)
        mark = "+" if status == 200 else "!"
        print(f"  {mark} {t['name']} -> {t['endpoint']} ({status})")

    _fix_block_policy()

    # Push config to the live gateway so it picks up the new endpoints + policy
    status, _ = _req("POST", f"{CONTROL_PLANE}/api/gateways/{GATEWAY_ID}/push")
    print(f"  pushed config to gateway ({status})")
    return 0


def _fix_block_policy() -> None:
    """Ensure crm-agent's block policy actually blocks the destructive demo tools.

    Two seeded issues break the Scenarios guard demo:
      1. Block patterns like '*.delete' don't fnmatch actions named 'db_delete'.
      2. A second active policy with 'block: []' clobbers the block list during
         the control plane's policy merge (dict.update).
    Fix the block-destructive policy's patterns and drop any empty block: [] from
    other active crm-agent policies so the merge can't wipe them.
    """
    status, policies = _req("GET", f"{CONTROL_PLANE}/api/policies")
    if status != 200 or not isinstance(policies, list):
        print("  ! could not read policies to fix block rules")
        return
    for p in policies:
        if p.get("gateway_id") != GATEWAY_ID:
            continue
        content = dict(p.get("content") or {})
        changed = False
        if p["name"] == "block-destructive":
            if content.get("block") != BLOCK_PATTERNS:
                content["block"] = BLOCK_PATTERNS
                changed = True
        elif "block" in content and not content["block"]:
            # empty block list would clobber block-destructive on merge
            content.pop("block")
            changed = True
        if changed:
            st, _ = _req("PATCH", f"{CONTROL_PLANE}/api/policies/{p['id']}", {"content": content})
            print(f"  {'+' if st == 200 else '!'} policy '{p['name']}' updated ({st})")


if __name__ == "__main__":
    sys.exit(main())
