"""Idempotent demo-data seeding for DB- and memory-backed resources.

The trace buffer is seeded in routers/traces.py. This covers metering (usage
records in the DB), A/B experiments (in-memory), and MCP server records (DB) so
those pages are populated on a fresh start. Runs from the app lifespan; only
seeds when empty, so it never duplicates or clobbers real data.

The seeded MCP servers are *real* stdio servers (draw.io + filesystem, run via
npx). The DB record is only the config; the crm-agent gateway actually spawns
the subprocess and discovers tools — at startup (from llm-gateway-config.yaml)
or when register_demo_mcp.py pushes them to a running gateway.
"""

from __future__ import annotations

import logging
import random
import shutil
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models.database import McpServer, PaymentRecord, UsageRecord, Wallet

log = logging.getLogger("control_plane.demo_seed")

_AGENTS_VOL = {
    "coder-agent": 200, "research-agent": 150, "analytics-agent": 90,
    "ops-agent": 70, "support-agent": 55, "db-agent": 40,
    "planner-agent": 30, "payments-agent": 12,
}
_MODELS = ["claude-haiku", "gpt-4o-mini", "claude-sonnet", "gpt-4o"]
_TOOLS = ["web_search", "db_query", "send_email", "github.search_code", "file_read"]


# Real MCP servers to seed on crm-agent. `command` is filled in at seed time
# from the resolved npx path (portable across machines). filesystem is sandboxed
# to a scratch dir so its tools actually execute end-to-end in the demo; draw.io
# discovers 28 real tools (calls need a browser-extension bridge).
DEMO_MCP_SANDBOX = "/tmp/ostiari-mcp-sandbox"
_MCP_SERVERS = [
    {
        "name": "drawio", "prefix": "drawio",
        "npx_args": ["-y", "drawio-mcp-server"],
    },
    {
        "name": "filesystem", "prefix": "fs",
        "npx_args": ["-y", "@modelcontextprotocol/server-filesystem", DEMO_MCP_SANDBOX],
    },
]


def demo_mcp_specs() -> list[dict]:
    """Real MCP server configs for the demo, or [] if npx isn't available.

    Shared by the DB seeder and register_demo_mcp.py so both agree on the
    exact command. Returns stdio configs with an absolute npx path.
    """
    npx = shutil.which("npx")
    if not npx:
        return []
    return [
        {
            "name": s["name"], "mode": "stdio", "prefix": s["prefix"],
            "command": [npx, *s["npx_args"]],
        }
        for s in _MCP_SERVERS
    ]


async def seed_demo_mcp(db: AsyncSession, gateway_id: str = "crm-agent") -> None:
    """Seed real MCP server records (idempotent; skips if any already exist)."""
    existing = (await db.execute(select(func.count()).select_from(McpServer))).scalar_one()
    if existing:
        return

    specs = demo_mcp_specs()
    if not specs:
        log.info("npx not found — skipping MCP server seed (install Node.js for the MCP demo)")
        return

    for spec in specs:
        db.add(McpServer(
            name=spec["name"], mode="stdio", prefix=spec["prefix"],
            command=spec["command"], gateway_id=gateway_id,
        ))
    await db.commit()
    log.info("Seeded %d real MCP server records (%s)", len(specs),
             ", ".join(s["name"] for s in specs))


async def seed_demo_db(db: AsyncSession) -> None:
    """Populate DB-backed demo data; idempotent (skips if already present)."""
    # Ensure the default org exists so org_id FKs resolve even when the admin
    # seed path (auth/router._seed_admin) hasn't run yet.
    from control_plane.models.database import DEFAULT_ORG, Organization
    if await db.get(Organization, DEFAULT_ORG) is None:
        db.add(Organization(id=DEFAULT_ORG, name="Default Organization"))
        await db.commit()

    await seed_demo_mcp(db)
    await seed_demo_payments(db)

    existing = (await db.execute(select(func.count()).select_from(UsageRecord))).scalar_one()
    if existing:
        return

    rnd = random.Random(42)
    now = datetime.now(timezone.utc)
    n = 0
    for agent, vol in _AGENTS_VOL.items():
        for _ in range(vol):
            db.add(UsageRecord(
                gateway_id=rnd.choice(["crm-agent", "ops-agent", "devops-agent", "analytics-agent"]),
                agent_id=agent, model=rnd.choice(_MODELS),
                input_tokens=rnd.randint(20, 600), output_tokens=rnd.randint(10, 600),
                total_tokens=rnd.randint(40, 1200), cost_usd=round(rnd.uniform(0.0003, 0.03), 5),
                action=rnd.choice(_TOOLS),
                timestamp=now - timedelta(minutes=rnd.randint(0, 60 * 24 * 20)),
            ))
            n += 1
    await db.commit()
    log.info("Seeded %d usage records for metering demo", n)


# Demo wallets: varied balances so the payments page tells a story — a nearly
# drained agent (blocks), a couple capped, the rest flush.
_WALLETS = [
    {"agent_id": "research-agent", "balance_usdc": 4.80, "daily_limit_usdc": 5.0},
    {"agent_id": "coder-agent", "balance_usdc": 9.20},
    {"agent_id": "analytics-agent", "balance_usdc": 2.15, "per_call_limit_usdc": 0.05},
    {"agent_id": "ops-agent", "balance_usdc": 6.00},
    {"agent_id": "support-agent", "balance_usdc": 1.10},
    {"agent_id": "db-agent", "balance_usdc": 0.80},
    {"agent_id": "planner-agent", "balance_usdc": 3.40},
    {"agent_id": "payments-agent", "balance_usdc": 0.002},  # nearly drained — blocks
]
_PAID_TOOLS = ["premium_search", "market_data.fetch", "enrichment.lookup", "geocode.resolve"]


async def seed_demo_payments(db: AsyncSession) -> None:
    """Seed x402 wallets + a ledger history (idempotent; skips if wallets exist)."""
    existing = (await db.execute(select(func.count()).select_from(Wallet))).scalar_one()
    if existing:
        return

    now = datetime.now(timezone.utc)
    for w in _WALLETS:
        db.add(Wallet(
            agent_id=w["agent_id"], balance_usdc=w["balance_usdc"],
            daily_limit_usdc=w.get("daily_limit_usdc"),
            per_call_limit_usdc=w.get("per_call_limit_usdc"),
            spent_today_usdc=round(w["balance_usdc"] * 0.0, 4),
        ))

    # Ledger history: mostly settled micropayments, a few blocked (drained agent).
    rnd = random.Random(7)
    n = 0
    funded = [w["agent_id"] for w in _WALLETS if w["balance_usdc"] > 0.5]
    for i in range(60):
        agent = rnd.choice(funded)
        amount = rnd.choice([0.005, 0.002, 0.01, 0.005])
        db.add(PaymentRecord(
            agent_id=agent, gateway_id="crm-agent", action=rnd.choice(_PAID_TOOLS),
            amount_usdc=amount, settled=True, tx_hash=f"sim-seed-{i}",
            mode="simulated", source="tool_402",
            timestamp=now - timedelta(minutes=rnd.randint(0, 60 * 24 * 7)),
        ))
        n += 1
    # A handful of blocked attempts from the drained agent.
    for i in range(5):
        db.add(PaymentRecord(
            agent_id="payments-agent", gateway_id="crm-agent",
            action="premium_search", amount_usdc=0.005, settled=False,
            mode="simulated", source="tool_402",
            timestamp=now - timedelta(minutes=rnd.randint(0, 60 * 24 * 3)),
        ))
        n += 1
    await db.commit()
    log.info("Seeded %d wallets and %d payment records", len(_WALLETS), n)


def seed_demo_pricing() -> None:
    """Set crm-agent's payment mode to passthrough (native x402) for the demo.

    Pricing lives in the payments router's in-memory policy; set it only if
    unset so a real configuration isn't overwritten.
    """
    from control_plane.routers.payments import _pricing

    if "crm-agent" in _pricing:
        return
    _pricing["crm-agent"] = {"mode": "passthrough", "default": 0.0, "overrides": {}}
    log.info("Seeded demo payment pricing (crm-agent: passthrough)")


def seed_demo_agents() -> None:
    """Seed the in-memory agent registry with the demo agents (idempotent)."""
    from control_plane.routers.agents import DEMO_AGENTS, _agents

    if _agents:
        return
    _agents.update(DEMO_AGENTS)
    log.info("Seeded %d demo agents", len(_agents))


def seed_demo_experiments() -> None:
    """Seed in-memory A/B experiments (persisted via the state file on shutdown)."""
    from control_plane.routers.experiments import ExperimentResponse, _experiments

    if _experiments:
        return
    for name, a, b, pct, gw in [
        ("haiku-vs-sonnet", "claude-haiku", "claude-sonnet", 30, "crm-agent"),
        ("gpt4o-vs-o3", "gpt-4o", "o3", 20, "crm-agent"),
        ("cost-routing-test", "gpt-4o-mini", "claude-haiku", 50, "ops-agent"),
    ]:
        _experiments[name] = ExperimentResponse(
            name=name, model_a=a, model_b=b, traffic_pct_b=pct, gateway_id=gw, is_active=True,
        )
    log.info("Seeded %d demo experiments", len(_experiments))
