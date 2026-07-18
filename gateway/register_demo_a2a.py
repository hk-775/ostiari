"""Register the demo's real A2A agent via the control plane.

Registers through the control plane (POST /api/a2a-agents/crm-agent), which both
connects the agent on the live gateway AND persists it — so it's reconnected
automatically on a gateway restart (no need to re-run this). Also gives it a
trust score so cross-agent delegation to it clears the seeded min_trust.

Idempotent. Run after the control plane, gateway, and A2A demo server are up:
    python register_demo_a2a.py
The Makefile runs this automatically as part of `make dev` / `make demo-full`.
"""

import json
import sys
import time
import urllib.error
import urllib.request

CONTROL_PLANE = "http://localhost:8400"
GATEWAY = "http://localhost:8421"
GATEWAY_ID = "crm-agent"
A2A_AGENT_URL = "http://localhost:9200"  # base URL; discovery adds /.well-known/agent.json

# Trust score for the demo A2A agent. The seeded cross-agent policy has
# min_trust=60, so this must clear it for the happy-path delegation to work.
DEMO_A2A_TRUST = 80


def _req(method: str, url: str, body: dict | None = None) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
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


def _set_trust(agent_key: str) -> None:
    """Give the connected A2A agent a trust score above the policy minimum."""
    status, policy = _req("GET", f"{GATEWAY}/config/cross-agent")
    if status != 200 or not isinstance(policy, dict):
        print("  ! could not read cross-agent policy to set trust")
        return
    policy.setdefault("trust_scores", {})[agent_key] = DEMO_A2A_TRUST
    st, _ = _req("POST", f"{GATEWAY}/config/cross-agent", policy)
    mark = "+" if st == 200 else "!"
    print(f"  {mark} trust[{agent_key}] = {DEMO_A2A_TRUST} ({st})")


def main() -> int:
    print("Registering real A2A agent via the control plane")
    if not _wait_for(f"{CONTROL_PLANE}/api/health", "control plane"):
        return 1
    if not _wait_for(f"{GATEWAY}/health", "crm-agent gateway"):
        return 1
    if not _wait_for(f"{A2A_AGENT_URL}/.well-known/agent.json", "A2A demo agent"):
        return 1

    # Register through the CP → connects on the gateway AND persists the record.
    status, resp = _req("POST", f"{CONTROL_PLANE}/api/a2a-agents/{GATEWAY_ID}",
                        {"url": A2A_AGENT_URL})
    if status == 200 and resp:
        key = resp.get("agent_key", "")
        print(f"  + {resp.get('name')}: registered as a2a.{key} (persisted), "
              f"skills: {', '.join(resp.get('skills', []))}")
        _set_trust(key)
    else:
        detail = (resp or {}).get("error") or (resp or {}).get("detail") or resp
        print(f"  ! registration failed: {status} {detail}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
