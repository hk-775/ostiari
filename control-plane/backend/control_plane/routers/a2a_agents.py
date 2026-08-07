"""A2A agent management API — persist registered agents so they survive a
gateway restart.

Registering an A2A agent both (a) connects it on the live gateway (discovery +
skill routing) and (b) records it in the control plane. On gateway restart, the
registration bundle carries these records and the gateway reconnects them — the
A2A analog of how MCP servers are restored.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import A2AAgentRecord, Gateway
from control_plane.models.scoping import get_scoped, scoped, stamp
from control_plane.services.push_service import gateway_config_headers

router = APIRouter(prefix="/api/a2a-agents", tags=["a2a-agents"])


class A2AAgentCreate(BaseModel):
    url: str
    name: str = ""
    auth_token: str = ""


@router.get("")
async def list_a2a_agents(gateway_id: str | None = None, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    query = scoped(select(A2AAgentRecord), A2AAgentRecord, org)
    if gateway_id:
        query = query.where(A2AAgentRecord.gateway_id == gateway_id)
    rows = (await db.execute(query)).scalars().all()
    return [_dict(r) for r in rows]


@router.post("/{gateway_id}")
async def register_a2a_agent(gateway_id: str, body: A2AAgentCreate, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Connect an A2A agent on the live gateway, then persist the record.

    We connect first (which discovers the agent card and gives us the stable
    agent_key), and only persist if the gateway accepted it — so we never store
    an agent that can't actually be reached.
    """
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")

    async with httpx.AsyncClient(
        timeout=15.0, headers=gateway_config_headers()
    ) as client:
        try:
            resp = await client.post(
                f"{gateway.endpoint}/config/a2a-agents",
                json={"url": body.url, "name": body.name, "auth_token": body.auth_token},
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Cannot reach gateway: {exc}") from None
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Gateway rejected agent: {resp.text[:200]}")
    connected = resp.json()
    agent_key = connected.get("agent_key", "")
    name = connected.get("name", body.name or agent_key)

    # Upsert by (gateway_id, agent_key).
    existing = (await db.execute(
        scoped(
            select(A2AAgentRecord).where(
                A2AAgentRecord.gateway_id == gateway_id,
                A2AAgentRecord.agent_key == agent_key,
            ),
            A2AAgentRecord,
            org,
        )
    )).scalar_one_or_none()
    if existing is None:
        rec = A2AAgentRecord(name=name, agent_key=agent_key, url=body.url,
                             auth_token=body.auth_token, gateway_id=gateway_id)
        stamp(rec, gateway.org_id)
        db.add(rec)
    else:
        existing.url = body.url
        existing.auth_token = body.auth_token
        existing.name = name
        rec = existing
    await db.commit()
    await db.refresh(rec)
    return {**_dict(rec), "skills": connected.get("skills", []), "tools": connected.get("tools", [])}


@router.delete("/{agent_id}")
async def delete_a2a_agent(agent_id: int, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    rec = await get_scoped(db, A2AAgentRecord, agent_id, org)
    if rec is None:
        raise HTTPException(status_code=404, detail="A2A agent not found")
    # Best-effort disconnect on the live gateway.
    gateway = await db.get(Gateway, rec.gateway_id)
    if gateway is not None:
        import contextlib
        async with httpx.AsyncClient(
            timeout=10.0, headers=gateway_config_headers()
        ) as client:
            with contextlib.suppress(Exception):  # record removal is what matters
                await client.delete(
                    f"{gateway.endpoint}/config/a2a-agents/{rec.agent_key}",
                )
    await db.delete(rec)
    await db.commit()
    return {"deleted": agent_id, "agent_key": rec.agent_key}


async def build_a2a_config(db: AsyncSession, gateway_id: str, org: str | None = None) -> list[dict]:
    """A2A agent connection configs for a gateway, for the registration bundle.

    Called from the unauthenticated gateway lifecycle (no user token), so `org`
    is optional; when provided it additionally scopes the query by tenant.
    """
    query = select(A2AAgentRecord).where(A2AAgentRecord.gateway_id == gateway_id)
    if org is not None:
        query = scoped(query, A2AAgentRecord, org)
    rows = (await db.execute(query)).scalars().all()
    return [{"url": r.url, "name": r.name, "auth_token": r.auth_token} for r in rows]


def _dict(r: A2AAgentRecord) -> dict:
    return {
        "id": r.id, "name": r.name, "agent_key": r.agent_key,
        "url": r.url, "gateway_id": r.gateway_id,
    }
