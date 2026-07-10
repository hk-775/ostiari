"""Gateway management API."""

from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.database import get_db
from control_plane.models.database import Gateway, Tool
from control_plane.models.schemas import GatewayCreate, GatewayResponse, GatewayUpdate
from control_plane.services.audit_service import audit
from control_plane.services.push_service import PushService

router = APIRouter(prefix="/api/gateways", tags=["gateways"])
push_service = PushService()


def _get_actor(request: Request) -> str:
    return request.headers.get("X-Actor", "system")


@router.get("", response_model=list[GatewayResponse])
async def list_gateways(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Gateway))
    gateways = result.scalars().all()
    responses = []
    for s in gateways:
        tools_result = await db.execute(select(Tool).where(Tool.gateway_id == s.id))
        tools_count = len(tools_result.scalars().all())
        responses.append(GatewayResponse(
            id=s.id, name=s.name, endpoint=s.endpoint,
            description=s.description, status=s.status,
            last_heartbeat=s.last_heartbeat, tools_count=tools_count,
            created_at=s.created_at, updated_at=s.updated_at,
        ))
    return responses


@router.post("", response_model=GatewayResponse)
async def register_gateway(body: GatewayCreate, request: Request, db: AsyncSession = Depends(get_db)):
    existing = await db.get(Gateway, body.id)
    if existing:
        raise HTTPException(status_code=409, detail=f"Gateway {body.id} already exists")
    gateway = Gateway(id=body.id, name=body.name, endpoint=body.endpoint, description=body.description)
    db.add(gateway)
    await audit.log(db, _get_actor(request), "create", "gateway", body.id, {"name": body.name, "endpoint": body.endpoint})
    await db.commit()
    await db.refresh(gateway)
    return GatewayResponse(
        id=gateway.id, name=gateway.name, endpoint=gateway.endpoint,
        description=gateway.description, status=gateway.status,
        last_heartbeat=gateway.last_heartbeat, tools_count=0,
        created_at=gateway.created_at, updated_at=gateway.updated_at,
    )


@router.get("/{gateway_id}", response_model=GatewayResponse)
async def get_gateway(gateway_id: str, db: AsyncSession = Depends(get_db)):
    gateway = await db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")
    tools_result = await db.execute(select(Tool).where(Tool.gateway_id == gateway_id))
    tools_count = len(tools_result.scalars().all())
    return GatewayResponse(
        id=gateway.id, name=gateway.name, endpoint=gateway.endpoint,
        description=gateway.description, status=gateway.status,
        last_heartbeat=gateway.last_heartbeat, tools_count=tools_count,
        created_at=gateway.created_at, updated_at=gateway.updated_at,
    )


@router.patch("/{gateway_id}", response_model=GatewayResponse)
async def update_gateway(gateway_id: str, body: GatewayUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    gateway = await db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(gateway, field, value)
    await audit.log(db, _get_actor(request), "update", "gateway", gateway_id, changes)
    await db.commit()
    await db.refresh(gateway)
    return await get_gateway(gateway_id, db)


@router.delete("/{gateway_id}")
async def delete_gateway(gateway_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    gateway = await db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")
    await audit.log(db, _get_actor(request), "delete", "gateway", gateway_id, {"name": gateway.name})
    await db.delete(gateway)
    await db.commit()
    return {"deleted": gateway_id}


@router.post("/{gateway_id}/push")
async def push_config(gateway_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Push current config to a specific gateway."""
    result = await push_service.push_to_gateway(db, gateway_id)
    await audit.log(db, _get_actor(request), "push", "gateway", gateway_id, {"status": result.status})
    await db.commit()
    if result.status == "error":
        raise HTTPException(status_code=502, detail=result.message)
    return result


@router.post("/push-all")
async def push_all(request: Request, db: AsyncSession = Depends(get_db)):
    """Push config to all registered gateways."""
    result = await push_service.push_to_all(db)
    await audit.log(db, _get_actor(request), "push_all", "gateway", "*", {"succeeded": result.succeeded, "failed": result.failed})
    await db.commit()
    return result


@router.get("/{gateway_id}/health")
async def check_health(gateway_id: str, db: AsyncSession = Depends(get_db)):
    """Check health of a gateway by calling its /health endpoint."""
    gateway = await db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{gateway.endpoint}/health")
            health = resp.json()
            gateway.status = "healthy"
            gateway.last_heartbeat = datetime.now(timezone.utc)
            await db.commit()
            return {"gateway_id": gateway_id, "status": "healthy", "details": health}
        except Exception as e:
            gateway.status = "unreachable"
            await db.commit()
            return {"gateway_id": gateway_id, "status": "unreachable", "error": str(e)}
