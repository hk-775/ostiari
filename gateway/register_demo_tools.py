"""Point crm-agent's demo tools at the demo tools server and add GitHub tools.

Idempotent: deletes any existing crm-agent tools with these names, re-adds them
pointing at http://localhost:9300, then pushes the config to the live gateway so
the Sandbox chat can call them and get useful canned data back.

Run after the control plane and demo_tools_server.py are up:
    python register_demo_tools.py
The Makefile runs this automatically as part of `make demo-full`.
"""

import os
import sys
import time
import urllib.error
import urllib.request

CONTROL_PLANE = os.environ.get("OSTIARI_CONTROL_PLANE_URL", "http://localhost:8400").rstrip("/")
GATEWAY_ID = os.environ.get("OSTIARI_GATEWAY_ID", "crm-agent")
DEMO_TOOLS = os.environ.get("OSTIARI_DEMO_TOOLS_URL", "http://localhost:9300").rstrip("/")

def _schema(*required: str, **optional: str) -> dict:
    """A JSON Schema for a flat string-parameter tool.

    Every tool needs one: an LLM driving the agentic loop only learns a tool's
    parameters from its schema. With none, the tool is advertised as taking no
    arguments and the model can't produce a usable call — the args named in the
    description are prose it has no contract for.
    """
    props = {name: {"type": "string", "description": desc}
             for name, desc in ((r, "") for r in required)}
    props.update({name: {"type": "string", "description": desc}
                  for name, desc in optional.items()})
    return {"type": "object", "properties": props, "required": list(required)}


TOOLS = [
    {"name": "web_search", "endpoint": f"{DEMO_TOOLS}/web_search", "method": "POST",
     "description": "Search the web for information on a topic.",
     "schema_json": _schema("query")},
    {"name": "db_query", "endpoint": f"{DEMO_TOOLS}/db_query", "method": "POST",
     "description": "Run a read-only SQL query against the app database.",
     "schema_json": _schema("sql")},
    {"name": "send_email", "endpoint": f"{DEMO_TOOLS}/send_email", "method": "POST",
     "description": "Send an email.",
     "schema_json": _schema("to", "subject", "body")},
    {"name": "github.list_repos", "endpoint": f"{DEMO_TOOLS}/github.list_repos", "method": "POST",
     "description": "List the user's GitHub repositories.",
     "schema_json": _schema()},
    {"name": "github.search_code", "endpoint": f"{DEMO_TOOLS}/github.search_code", "method": "POST",
     "description": "Search code across GitHub repositories.",
     "schema_json": _schema("query")},
    {"name": "github.create_issue", "endpoint": f"{DEMO_TOOLS}/github.create_issue", "method": "POST",
     "description": "Open a GitHub issue.",
     "schema_json": _schema("repo", "title", "body")},
    {"name": "drawio.list_diagrams", "endpoint": f"{DEMO_TOOLS}/drawio.list_diagrams", "method": "POST",
     "description": "List draw.io diagrams.",
     "schema_json": _schema()},
    {"name": "drawio.create_diagram", "endpoint": f"{DEMO_TOOLS}/drawio.create_diagram", "method": "POST",
     "description": "Create a draw.io diagram.",
     "schema_json": _schema("name")},
    {"name": "drawio.add_shape", "endpoint": f"{DEMO_TOOLS}/drawio.add_shape", "method": "POST",
     "description": "Add a shape to a diagram.",
     "schema_json": _schema("diagram_id", "shape", "label")},
    # Destructive tools — registered so the Scenarios demo exercises the policy
    # guard (the block-destructive policy blocks these before they run).
    {"name": "db_delete", "endpoint": f"{DEMO_TOOLS}/db_delete", "method": "POST",
     "description": "Delete rows from a database table.",
     "schema_json": _schema("table")},
    {"name": "github.delete_repo", "endpoint": f"{DEMO_TOOLS}/github.delete_repo", "method": "POST",
     "description": "Delete a GitHub repository.",
     "schema_json": _schema("repo")},
    {"name": "drawio.delete_diagram", "endpoint": f"{DEMO_TOOLS}/drawio.delete_diagram", "method": "POST",
     "description": "Delete a draw.io diagram.",
     "schema_json": _schema("id")},
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
    """Ensure crm-agent has a block policy that stops the destructive demo tools.

    This registers the destructive tools (db_delete, github.delete_repo,
    drawio.delete_diagram) so the Scenarios tab can demonstrate the guard
    stopping them — which only works if a policy actually blocks them.

    Creates the policy if it's missing. It used to only patch an existing one,
    which meant the guard demo depended on a row someone had added by hand in an
    earlier session: recreate the database and crm-agent came up with an empty
    block list, so the destructive scenarios silently *executed* while the
    Policies page showed nothing for this gateway.

    Two further issues break the guard even when the policy is present:
      1. Block patterns like '*.delete' don't fnmatch actions named 'db_delete'.
      2. A second active policy with 'block: []' clobbers the block list during
         the control plane's policy merge (dict.update).
    So also fix the patterns, and drop any empty block: [] from other active
    crm-agent policies so the merge can't wipe them.
    """
    status, policies = _req("GET", f"{CONTROL_PLANE}/api/policies")
    if status != 200 or not isinstance(policies, list):
        print("  ! could not read policies to fix block rules")
        return

    mine = [p for p in policies if p.get("gateway_id") == GATEWAY_ID]

    if not any(p["name"] == "block-destructive" for p in mine):
        st, _ = _req("POST", f"{CONTROL_PLANE}/api/policies", {
            "name": "block-destructive",
            "description": "Block destructive tool calls (delete/drop/destroy) on crm-agent",
            "content": {"block": BLOCK_PATTERNS},
            "gateway_id": GATEWAY_ID,
        })
        print(f"  {'+' if st == 200 else '!'} policy 'block-destructive' created ({st})")

    for p in mine:
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
