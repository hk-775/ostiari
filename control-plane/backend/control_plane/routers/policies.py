"""Policy management API."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import get_db
from control_plane.models.database import Gateway, Policy
from control_plane.models.schemas import (
    PolicyCreate,
    PolicyResponse,
    PolicyUpdate,
    PushResponse,
)
from control_plane.models.scoping import get_scoped, scoped, stamp
from control_plane.services.audit_service import actor_of, audit
from control_plane.services.push_service import PushService

router = APIRouter(prefix="/api/policies", tags=["policies"])
push_service = PushService()


@router.get("", response_model=list[PolicyResponse])
async def list_policies(db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    result = await db.execute(scoped(select(Policy), Policy, org))
    return result.scalars().all()


@router.post("", response_model=PolicyResponse)
async def create_policy(body: PolicyCreate, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    if body.gateway_id and not await get_scoped(
        db, Gateway, body.gateway_id, org
    ):
        raise HTTPException(status_code=404, detail="Gateway not found")
    policy = Policy(
        name=body.name, description=body.description,
        content=body.content, gateway_id=body.gateway_id,
    )
    stamp(policy, org)
    db.add(policy)
    await audit.log(db, actor_of(request), "create", "policy", body.name, {"gateway_id": body.gateway_id}, org=org)
    await db.commit()
    await db.refresh(policy)
    return policy


@router.get("/{policy_id}", response_model=PolicyResponse)
async def get_policy(policy_id: int, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    policy = await get_scoped(db, Policy, policy_id, org)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    return policy


@router.patch("/{policy_id}", response_model=PolicyResponse)
async def update_policy(policy_id: int, body: PolicyUpdate, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    policy = await get_scoped(db, Policy, policy_id, org)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(policy, field, value)
    await audit.log(db, actor_of(request), "update", "policy", str(policy_id), changes, org=org)
    await db.commit()
    await db.refresh(policy)
    return policy


@router.delete("/{policy_id}")
async def delete_policy(policy_id: int, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    policy = await get_scoped(db, Policy, policy_id, org)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    await audit.log(db, actor_of(request), "delete", "policy", str(policy_id), {"name": policy.name}, org=org)
    await db.delete(policy)
    await db.commit()
    return {"deleted": policy_id}


@router.post("/{policy_id}/push", response_model=PushResponse)
async def push_policy(policy_id: int, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Push the effective policy set to the assigned gateway, or all if global."""
    policy = await get_scoped(db, Policy, policy_id, org)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    if policy.gateway_id:
        gateway = await get_scoped(db, Gateway, policy.gateway_id, org)
        if not gateway:
            raise HTTPException(status_code=404, detail="Gateway not found")
        result = await push_service.push_policy(db, gateway)
        response = PushResponse(
            results=[result],
            total=1,
            succeeded=int(result.status == "success"),
            failed=int(result.status != "success"),
        )
    else:
        response = await push_service.push_policy_to_all(db, org)
    await audit.log(
        db,
        actor_of(request),
        "push",
        "policy",
        str(policy_id),
        {
            "gateway_id": policy.gateway_id,
            "total": response.total,
            "succeeded": response.succeeded,
            "failed": response.failed,
        },
        org=org,
    )
    await db.commit()
    return response
