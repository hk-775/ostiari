"""Durable gateway routing and budget-period controls."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.budget_reset import next_reset_at
from control_plane.database import get_db
from control_plane.models.database import Gateway
from control_plane.models.scoping import get_scoped
from control_plane.services.audit_service import actor_of, audit
from control_plane.services.push_service import gateway_config_headers

router = APIRouter(prefix="/api/routing-controls", tags=["routing-controls"])


class TaskClassificationConfig(BaseModel):
    rules: dict[str, list[str]] = Field(default_factory=dict)
    model_mapping: dict[str, str] = Field(default_factory=dict)


class BudgetResetConfig(BaseModel):
    schedule: Literal["manual", "daily", "weekly", "monthly"] = "manual"


def _budget_state(gateway: Gateway) -> dict[str, Any]:
    stored = (gateway.config or {}).get("budget_reset", {})
    schedule = stored.get("schedule", "manual")
    next_reset = next_reset_at(schedule)
    return {
        "schedule": schedule,
        "last_reset_at": stored.get("last_reset_at"),
        "configured_at": stored.get("configured_at"),
        "next_reset": next_reset.isoformat() if next_reset else None,
    }


async def _push(
    gateway: Gateway,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[bool, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=10.0, headers=gateway_config_headers()
        ) as client:
            response = await client.post(
                f"{gateway.endpoint}{path}",
                json=payload,
            )
        if response.status_code == 200:
            return True, response.json()
        return False, f"HTTP {response.status_code}: {response.text[:200]}"
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return False, str(exc)


@router.get("/{gateway_id}")
async def get_controls(
    gateway_id: str,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict[str, Any]:
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")
    return {
        "gateway_id": gateway.id,
        "budget_reset": _budget_state(gateway),
        "task_classification": (gateway.config or {}).get(
            "task_classification",
            {"rules": {}, "model_mapping": {}},
        ),
    }


@router.put("/{gateway_id}/task-classification")
async def set_task_classification(
    gateway_id: str,
    body: TaskClassificationConfig,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict[str, Any]:
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")

    from control_plane.routers.model_config import _models

    unknown = sorted({
        model
        for model in body.model_mapping.values()
        if model and model not in _models[org]
    })
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown target models: {', '.join(unknown)}",
        )

    rules = {
        category.strip().lower().replace(" ", "_"): list(dict.fromkeys(
            keyword.strip().lower()
            for keyword in keywords
            if keyword.strip()
        ))
        for category, keywords in body.rules.items()
        if category.strip()
    }
    config = {
        "rules": rules,
        "model_mapping": {
            category.strip().lower().replace(" ", "_"): model
            for category, model in body.model_mapping.items()
            if category.strip() and model
        },
    }
    gateway.config = {
        **(gateway.config or {}),
        "task_classification": config,
    }
    await audit.log(
        db,
        actor_of(request),
        "update",
        "task_classification",
        gateway_id,
        {"categories": sorted(rules), "models": config["model_mapping"]},
        org=org,
    )
    await db.commit()

    pushed, detail = await _push(
        gateway,
        "/config/task-classification",
        config,
    )
    return {
        "status": "saved",
        "pushed": pushed,
        "push_error": None if pushed else detail,
        "config": config,
    }


@router.put("/{gateway_id}/budget-reset")
async def set_budget_reset(
    gateway_id: str,
    body: BudgetResetConfig,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict[str, Any]:
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")

    existing = (gateway.config or {}).get("budget_reset", {})
    last_reset_at = existing.get("last_reset_at")
    configured_at = existing.get("configured_at")
    if existing.get("schedule") != body.schedule or not configured_at:
        # This is the baseline for catch-up logic. Enabling a schedule should
        # wait for its next boundary, not erase the current period immediately.
        configured_at = datetime.now(timezone.utc).isoformat()
    config = {
        "schedule": body.schedule,
        "last_reset_at": last_reset_at,
        "configured_at": configured_at,
    }
    gateway.config = {**(gateway.config or {}), "budget_reset": config}
    await audit.log(
        db,
        actor_of(request),
        "update",
        "budget_reset",
        gateway_id,
        {"schedule": body.schedule},
        org=org,
    )
    await db.commit()

    pushed, detail = await _push(gateway, "/config/budget-reset", config)
    next_reset = next_reset_at(body.schedule)
    state = {
        **config,
        "next_reset": next_reset.isoformat() if next_reset else None,
    }
    return {
        "status": "saved",
        "pushed": pushed,
        "push_error": None if pushed else detail,
        "config": state,
    }


@router.post("/{gateway_id}/reset-spend")
async def reset_spend_now(
    gateway_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
) -> dict[str, Any]:
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")
    pushed, detail = await _push(gateway, "/config/quota/reset-spend")
    if not pushed:
        raise HTTPException(status_code=502, detail=str(detail))
    reset_at = (
        detail.get("last_reset_at")
        if isinstance(detail, dict)
        else None
    ) or datetime.now(timezone.utc).isoformat()
    config = {**(gateway.config or {})}
    config["agent_spend"] = dict.fromkeys(config.get("agent_spend", {}), 0.0)
    config["budget_reset"] = {
        **config.get("budget_reset", {}),
        "last_reset_at": reset_at,
    }
    gateway.config = config
    await audit.log(
        db,
        actor_of(request),
        "reset",
        "budget_spend",
        gateway_id,
        {},
        org=org,
    )
    await db.commit()
    return {
        "status": "reset",
        "gateway_id": gateway_id,
        "last_reset_at": reset_at,
        "gateway": detail,
    }
