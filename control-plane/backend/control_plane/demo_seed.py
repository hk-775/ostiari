"""Idempotent demo-data seeding for DB- and memory-backed resources.

The trace buffer is seeded in routers/traces.py. This covers metering (usage
records in the DB) and A/B experiments (in-memory) so those pages are populated
on a fresh start. Runs from the app lifespan; only seeds when empty, so it
never duplicates or clobbers real data.

MCP servers are intentionally not seeded here: they carry a gateway_id foreign
key to gateways.id, which only exist once a real gateway registers at runtime —
seeding them at startup would create dangling references.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.models.database import UsageRecord

log = logging.getLogger("control_plane.demo_seed")

_AGENTS_VOL = {
    "coder-agent": 200, "research-agent": 150, "analytics-agent": 90,
    "ops-agent": 70, "support-agent": 55, "db-agent": 40,
    "planner-agent": 30, "payments-agent": 12,
}
_MODELS = ["claude-haiku", "gpt-4o-mini", "claude-sonnet", "gpt-4o"]
_TOOLS = ["web_search", "db_query", "send_email", "github.search_code", "file_read"]


async def seed_demo_db(db: AsyncSession) -> None:
    """Populate DB-backed demo data; idempotent (skips if already present)."""
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
