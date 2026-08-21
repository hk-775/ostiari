"""Demo deployments must seed real, FK-safe gateway and tool contracts."""

import pytest
from sqlalchemy import func, select

pytestmark = pytest.mark.anyio


async def test_demo_seed_creates_gateway_parents_tools_and_policy(app_and_db, monkeypatch):
    from control_plane.database import async_session
    from control_plane.demo_seed import (
        _DEMO_GATEWAYS,
        _DEMO_TOOL_DEFINITIONS,
        seed_demo_db,
    )
    from control_plane.models.database import Gateway, Policy, Tool

    monkeypatch.setenv("OSTIARI_DEMO_GATEWAY_URL", "http://gateway.internal:8421")
    monkeypatch.setenv("OSTIARI_DEMO_TOOLS_URL", "http://demo.internal:9300")

    async with async_session() as db:
        await seed_demo_db(db)
        await seed_demo_db(db)
        gateways = (await db.execute(select(Gateway))).scalars().all()
        tools = (await db.execute(select(Tool))).scalars().all()
        policies = (await db.execute(select(Policy))).scalars().all()
        gateway_count = (await db.execute(select(func.count()).select_from(Gateway))).scalar_one()

    assert gateway_count == len(_DEMO_GATEWAYS)
    crm = next(gateway for gateway in gateways if gateway.id == "crm-agent")
    assert crm.endpoint == "http://gateway.internal:8421"
    assert len(tools) == len(_DEMO_TOOL_DEFINITIONS)
    assert all(tool.endpoint.startswith("http://demo.internal:9300/") for tool in tools)
    assert {"db_delete", "drawio.delete_diagram", "market_data.fetch"} <= {
        tool.name for tool in tools
    }
    assert any(policy.name == "block-destructive" for policy in policies)
