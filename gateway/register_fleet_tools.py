"""Give every demo gateway a working, role-appropriate toolset.

register_demo_tools.py fully wires crm-agent (customer/research team). The
other three gateways were seeded with placeholder tools pointing at dead
endpoints (localhost:8080, mcp://...), so they showed up but didn't work. This
points each at the live demo tools server (:9300) with a distinct role, so the
whole fleet reads like a real multi-team deployment where every gateway's tools
actually return data.

Idempotent: removes existing same-named tools per gateway, re-adds pointing at
:9300, pushes config. Run after the control plane + demo tools server + the
gateways are up (the Makefile runs it in demo-full).
"""

import sys
import time
import urllib.error
import urllib.request

CONTROL_PLANE = "http://localhost:8400"
DEMO_TOOLS = "http://localhost:9300"

# Each gateway gets a role-appropriate set of tools. All point at real demo
# endpoints so calls return canned-but-useful data. Destructive tools are
# included where the role would plausibly have them, to exercise policy blocks.
# Block patterns that must catch the destructive demo tools. Note db_delete has
# no dot, so "*.delete" won't match it — patterns must be explicit (same lesson
# as the crm-agent block policy).
_BLOCK = ["*delete*", "*.drop", "*.destroy", "db_delete"]

FLEET = {
    "ops-agent": {
        "role": "Operations",
        "tools": [
            ("db_query", "/db_query", "Query the ops database. Args: sql."),
            ("send_email", "/send_email", "Send an ops notification email. Args: to, subject, body."),
            ("slack.post", "/send_email", "Post to an ops Slack channel. Args: channel, text."),
            ("db_delete", "/db_delete", "Delete rows from a table. Args: table."),
        ],
        "policy": {"name": "ops-guard", "block": _BLOCK, "allow": ["db_query", "send_email"]},
    },
    "devops-agent": {
        "role": "DevOps / CI-CD",
        "tools": [
            ("github.search_code", "/github.search_code", "Search code across repos. Args: query."),
            ("github.create_issue", "/github.create_issue", "Open a GitHub issue. Args: repo, title, body."),
            ("web_search", "/web_search", "Search docs/runbooks. Args: query."),
            ("github.delete_repo", "/github.delete_repo", "Delete a repository. Args: repo."),
        ],
        # This used to be None, on a comment claiming a 'devops-strict' policy
        # already blocked delete_repo. Nothing in the repo ever created one, so a
        # fresh demo left github.delete_repo callable — a governance product
        # shipping an ungoverned destructive tool.
        "policy": {
            "name": "devops-guard", "block": _BLOCK,
            "allow": ["github.search_code", "github.create_issue", "web_search"],
        },
    },
    "analytics-agent": {
        "role": "Analytics",
        "tools": [
            ("db_query", "/db_query", "Query the analytics warehouse. Args: sql."),
            ("web_search", "/web_search", "Research market/benchmark data. Args: query."),
            ("drawio.create_diagram", "/drawio.create_diagram", "Create a chart/diagram. Args: name."),
        ],
        "policy": None,  # analytics has no destructive tools
    },
}


def _req(method: str, url: str, body: dict | None = None) -> tuple[int, object]:
    import json
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
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
            if _req("GET", url)[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    print(f"  ! {label} not reachable at {url} after {tries}s")
    return False


def main() -> int:
    print("Provisioning working toolsets across the fleet")
    if not _wait_for(f"{CONTROL_PLANE}/api/health", "control plane"):
        return 1
    if not _wait_for(f"{DEMO_TOOLS}/health", "demo tools server"):
        return 1

    for gw, spec in FLEET.items():
        names = {t[0] for t in spec["tools"]}
        # Remove any existing same-named tools (idempotent; clears dead stubs too).
        status, existing = _req("GET", f"{CONTROL_PLANE}/api/tools?gateway_id={gw}")
        if status == 200 and isinstance(existing, list):
            for t in existing:
                if t["name"] in names:
                    _req("DELETE", f"{CONTROL_PLANE}/api/tools/{t['id']}")
        added = 0
        for name, path, desc in spec["tools"]:
            st, _ = _req("POST", f"{CONTROL_PLANE}/api/tools/{gw}", {
                "name": name, "endpoint": f"{DEMO_TOOLS}{path}", "method": "POST",
                "description": desc,
            })
            if st == 200:
                added += 1
        pol = _ensure_policy(gw, spec.get("policy"))
        _req("POST", f"{CONTROL_PLANE}/api/gateways/{gw}/push")
        print(f"  + {gw} ({spec['role']}): {added}/{len(spec['tools'])} tools{pol} → pushed")

    return 0


def _ensure_policy(gw: str, policy: dict | None) -> str:
    """Create/update a gateway-scoped block policy so destructive tools are governed."""
    if not policy:
        return ""
    name = policy["name"]
    content = {"block": policy["block"], "allow": policy.get("allow", [])}
    status, existing = _req("GET", f"{CONTROL_PLANE}/api/policies")
    pid = None
    if status == 200 and isinstance(existing, list):
        for p in existing:
            if p["name"] == name and p.get("gateway_id") == gw:
                pid = p["id"]
                break
    if pid is None:
        st, _ = _req("POST", f"{CONTROL_PLANE}/api/policies",
                     {"name": name, "content": content, "gateway_id": gw})
    else:
        st, _ = _req("PATCH", f"{CONTROL_PLANE}/api/policies/{pid}", {"content": content})
    return f" + policy '{name}'" if st == 200 else f" (policy '{name}' failed)"


if __name__ == "__main__":
    sys.exit(main())
