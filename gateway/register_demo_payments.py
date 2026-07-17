"""Register the demo's paywalled tools and push x402 payment config.

The payment gate demo (native x402 passthrough) needs a tool that returns HTTP
402. demo_tools_server.py serves /premium_search for exactly this. This script
registers it (and a couple of aliases) as crm-agent tools via the control
plane, then pushes the payment config (passthrough mode + seeded wallets) to
the live gateway so paid calls settle against agent wallets.

Idempotent. Run after the control plane, demo tools server, and crm-agent
gateway are up:
    python register_demo_payments.py
The Makefile runs this automatically as part of `make demo-full`.
"""

import json
import sys
import time
import urllib.error
import urllib.request

CONTROL_PLANE = "http://localhost:8400"
GATEWAY_ID = "crm-agent"
DEMO_TOOLS = "http://localhost:9300"

# Paywalled tools. All point at the one 402-returning demo endpoint; distinct
# names give the ledger some variety.
PAID_TOOLS = [
    {"name": "premium_search", "endpoint": f"{DEMO_TOOLS}/premium_search", "method": "POST",
     "description": "Paid premium search (x402). Args: query (string)."},
    {"name": "market_data.fetch", "endpoint": f"{DEMO_TOOLS}/premium_search", "method": "POST",
     "description": "Paid market data feed (x402). Args: query (string)."},
]


def _req(method: str, url: str, body: dict | None = None,
         headers: dict | None = None) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method=method)
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


def _token() -> str:
    status, body = _req("POST", f"{CONTROL_PLANE}/api/auth/login",
                        {"email": "admin@ostiari.ai", "password": "admin"})
    if status == 200 and body:
        return body.get("access_token", "")
    return ""


def main() -> int:
    print("Registering paywalled tools + payment config on", GATEWAY_ID)
    if not _wait_for(f"{CONTROL_PLANE}/api/health", "control plane"):
        return 1
    if not _wait_for(f"{DEMO_TOOLS}/health", "demo tools server"):
        return 1
    if not _wait_for("http://localhost:8421/health", "crm-agent gateway"):
        return 1

    tok = _token()
    auth = {"Authorization": f"Bearer {tok}"} if tok else {}

    # Register the paid tools (idempotent: remove existing with same names first).
    names = {t["name"] for t in PAID_TOOLS}
    status, existing = _req("GET", f"{CONTROL_PLANE}/api/tools?gateway_id={GATEWAY_ID}", headers=auth)
    if status == 200 and isinstance(existing, list):
        for t in existing:
            if t["name"] in names:
                _req("DELETE", f"{CONTROL_PLANE}/api/tools/{t['id']}", headers=auth)
    for t in PAID_TOOLS:
        st, _ = _req("POST", f"{CONTROL_PLANE}/api/tools/{GATEWAY_ID}", t, headers=auth)
        print(f"  {'+' if st == 200 else '!'} tool {t['name']} ({st})")

    # Push tool config to the gateway, then push payment config (mode + wallets).
    _req("POST", f"{CONTROL_PLANE}/api/gateways/{GATEWAY_ID}/push", headers=auth)
    st, resp = _req("POST", f"{CONTROL_PLANE}/api/payments/push?gateway_id={GATEWAY_ID}", headers=auth)
    if st == 200 and resp:
        print(f"  + payment config pushed: {resp.get('wallets', 0)} wallets, passthrough mode")
    else:
        print(f"  ! payment push failed: {st} {resp}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
