"""Demo deployments must seed real, FK-safe gateway and tool contracts."""

import pytest
from sqlalchemy import func, select

pytestmark = pytest.mark.anyio


async def test_demo_seed_creates_gateway_parents_tools_and_policy(app_and_db, monkeypatch):
    from control_plane.database import async_session
    from control_plane.demo_seed import (
        _DEMO_GATEWAYS,
        _DEMO_TOOL_DEFINITIONS,
        _REMOTE_MCP_TOOL_NAMES,
        seed_demo_db,
    )
    from control_plane.models.database import Gateway, Policy, Tool

    monkeypatch.setenv("OSTIARI_DEMO_GATEWAY_URL", "http://gateway.internal:8421")
    monkeypatch.setenv("OSTIARI_DEMO_GATEWAY_DOMAIN", "evaluation.ostiari.local")
    monkeypatch.setenv("OSTIARI_DEMO_TOOLS_URL", "http://demo.internal:9300")
    monkeypatch.setenv("OSTIARI_DEMO_MCP_BASE_URL", "http://demo.internal:9300/mcp")
    monkeypatch.setenv("OSTIARI_DEMO_A2A_BASE_URL", "http://demo.internal:9300")

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
    ops = next(gateway for gateway in gateways if gateway.id == "ops-agent")
    assert ops.endpoint == "http://ops-agent.evaluation.ostiari.local:8421"
    assert len(tools) == (
        len(_DEMO_TOOL_DEFINITIONS) - len(_REMOTE_MCP_TOOL_NAMES)
    ) * len(_DEMO_GATEWAYS)
    assert all(tool.endpoint.startswith("http://demo.internal:9300/") for tool in tools)
    assert {"db_delete", "market_data.fetch"} <= {
        tool.name for tool in tools
    }
    assert not _REMOTE_MCP_TOOL_NAMES.intersection(tool.name for tool in tools)
    assert len(policies) == len(_DEMO_GATEWAYS)
    assert any(policy.name == "block-destructive" for policy in policies)


async def test_demo_seed_connects_remote_mcp_and_a2a_to_every_gateway(
    app_and_db,
    monkeypatch,
):
    from control_plane.database import async_session
    from control_plane.demo_seed import _DEMO_GATEWAYS, seed_demo_db
    from control_plane.models.database import A2AAgentRecord, McpServer

    monkeypatch.setenv("OSTIARI_DEMO_MCP_BASE_URL", "http://demo.internal:9300/mcp")
    monkeypatch.setenv("OSTIARI_DEMO_A2A_BASE_URL", "http://demo.internal:9300")

    async with async_session() as db:
        await seed_demo_db(db)
        mcp = (await db.execute(select(McpServer))).scalars().all()
        a2a = (await db.execute(select(A2AAgentRecord))).scalars().all()

    assert len(mcp) == 2 * len(_DEMO_GATEWAYS)
    assert {server.mode for server in mcp} == {"remote"}
    assert {server.name for server in mcp} == {"drawio", "filesystem"}
    assert all(server.url.startswith("http://demo.internal:9300/mcp/") for server in mcp)
    assert len(a2a) == len(_DEMO_GATEWAYS)
    assert {record.agent_key for record in a2a} == {"devops_assistant"}
