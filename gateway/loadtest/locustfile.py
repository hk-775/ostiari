"""Locust load profile for the Ostiari gateway.

Stress-tests the hot paths with realistic mixes. Run against a gateway you
started yourself (see loadtest/README.md):

    locust -f gateway/loadtest/locustfile.py --host http://localhost:8421

Then open http://localhost:8089 and set users / spawn-rate, or run headless:

    locust -f gateway/loadtest/locustfile.py --host http://localhost:8421 \
        --headless -u 200 -r 50 -t 2m

Weights favor /validate (pure guard-engine overhead, no downstream call) and
/health (heartbeat volume), with a slice of real /tool proxying and a policy
block case so you exercise both allow and deny paths under load.
"""

from __future__ import annotations

import random

from locust import HttpUser, between, task

# A spread of agent ids so per-agent state (auth, quotas, cross-agent) is
# exercised rather than a single hot key.
_AGENTS = [f"load-agent-{i}" for i in range(50)]

# Tools the full-demo stack registers on crm-agent (:8421). Adjust to match
# your gateway's actual catalog if you're testing a different one.
_ALLOWED_TOOLS = ["web_search", "db_query", "send_email"]
_BLOCKED_TOOLS = ["db_delete", "github.delete_repo"]  # matched by *.delete etc.


def _headers() -> dict:
    return {"X-Agent-Id": random.choice(_AGENTS), "X-Framework": "loadtest"}


class GatewayUser(HttpUser):
    # Think-time between a user's requests; keep small for throughput tests,
    # raise for a steady-state soak.
    wait_time = between(0.0, 0.05)

    @task(10)
    def validate_allow(self) -> None:
        """Pure policy evaluation — the cleanest measure of Ostiari overhead."""
        self.client.post(
            "/validate",
            json={"action": random.choice(_ALLOWED_TOOLS), "params": {"q": "x"}},
            headers=_headers(),
            name="POST /validate (allow)",
        )

    @task(3)
    def validate_block(self) -> None:
        """Exercise the block path (score/threshold + block glob)."""
        self.client.post(
            "/validate",
            json={"action": random.choice(_BLOCKED_TOOLS), "params": {"table": "users"}},
            headers=_headers(),
            name="POST /validate (block)",
        )

    @task(6)
    def health(self) -> None:
        """Heartbeat/health volume — what the control plane polls."""
        self.client.get("/health", name="GET /health")

    @task(2)
    def list_tools(self) -> None:
        self.client.get("/tools", name="GET /tools")

    @task(1)
    def proxy_tool(self) -> None:
        """Full gate chain + real downstream proxy. Only meaningful if the tool's
        endpoint (e.g. demo_tools_server :9300) is up; otherwise it 502s, which
        is itself a useful upstream-failure load signal."""
        self.client.post(
            f"/tool/{random.choice(_ALLOWED_TOOLS)}",
            json={"q": "load", "to": "x@example.com", "body": "hi"},
            headers=_headers(),
            name="POST /tool/{action}",
        )
