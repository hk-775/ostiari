"""Intervention API router."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ostiari.dashboard.dependencies import get_intervention_broker
from ostiari.dashboard.intervention import InterventionBroker

router = APIRouter(prefix="/api/intervention", tags=["intervention"])


class InterventionResponse(BaseModel):
    approved: bool


@router.post("/{request_id}/respond")
async def respond_to_intervention(
    request_id: str,
    body: InterventionResponse,
    broker: InterventionBroker | None = Depends(get_intervention_broker),
) -> dict[str, Any]:
    if broker is None:
        raise HTTPException(
            status_code=503,
            detail="Intervention broker unavailable (Redis not configured)",
        )

    accepted = await broker.respond(request_id, body.approved)
    if not accepted:
        raise HTTPException(status_code=409, detail="Intervention already handled")

    return {"status": "accepted"}
