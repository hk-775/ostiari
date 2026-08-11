"""Browser-isolated Sandbox run lifecycle and governed tool bridge."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org, get_current_user
from control_plane.database import get_db
from control_plane.models.database import Gateway, SandboxRun
from control_plane.models.scoping import get_scoped
from control_plane.services.audit_service import actor_of, audit

router = APIRouter(prefix="/api/sandbox/runs", tags=["sandbox"])

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_FINAL_STATUSES = {"completed", "error", "cancelled", "timed_out"}


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gateway_id: str = Field(min_length=1, max_length=64)
    language: Literal["javascript"] = "javascript"
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_bytes: int = Field(ge=0)


class RunComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "error", "cancelled", "timed_out"]
    duration_ms: int = Field(ge=0, le=300_000)
    output_bytes: int = Field(ge=0)
    error: str = Field(default="", max_length=512)


class RunResponse(BaseModel):
    id: str
    gateway_id: str
    language: str
    source_digest: str
    source_bytes: int
    status: str
    timeout_ms: int
    max_tool_calls: int
    max_output_bytes: int
    max_tool_payload_bytes: int
    tool_calls: int
    output_bytes: int
    error: str
    started_at: datetime
    completed_at: datetime | None


class _Limits(BaseModel):
    max_source_bytes: int
    timeout_ms: int
    max_tool_calls: int
    max_output_bytes: int
    max_tool_payload_bytes: int
    max_active_runs: int


def _env_int(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(lower, min(value, upper))


def _limits() -> _Limits:
    """Server-owned bounds; environment values can only move within safe caps."""
    return _Limits(
        max_source_bytes=_env_int("OSTIARI_SANDBOX_MAX_SOURCE_BYTES", 32 * 1024, 1024, 128 * 1024),
        timeout_ms=_env_int("OSTIARI_SANDBOX_TIMEOUT_MS", 10_000, 1_000, 60_000),
        max_tool_calls=_env_int("OSTIARI_SANDBOX_MAX_TOOL_CALLS", 20, 1, 100),
        max_output_bytes=_env_int("OSTIARI_SANDBOX_MAX_OUTPUT_BYTES", 64 * 1024, 1024, 256 * 1024),
        max_tool_payload_bytes=_env_int(
            "OSTIARI_SANDBOX_MAX_TOOL_PAYLOAD_BYTES", 16 * 1024, 256, 64 * 1024
        ),
        max_active_runs=_env_int("OSTIARI_SANDBOX_MAX_ACTIVE_RUNS", 4, 1, 20),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _deadline(run: SandboxRun) -> datetime:
    return _as_utc(run.started_at) + timedelta(milliseconds=run.timeout_ms)


def _response(run: SandboxRun) -> RunResponse:
    return RunResponse(
        id=run.id,
        gateway_id=run.gateway_id,
        language=run.language,
        source_digest=run.source_digest,
        source_bytes=run.source_bytes,
        status=run.status,
        timeout_ms=run.timeout_ms,
        max_tool_calls=run.max_tool_calls,
        max_output_bytes=run.max_output_bytes,
        max_tool_payload_bytes=run.max_tool_payload_bytes,
        tool_calls=run.tool_calls,
        output_bytes=run.output_bytes,
        error=run.error,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


async def _actor(request: Request) -> str:
    if request.headers.get("Authorization", "").startswith("Bearer "):
        try:
            return (await get_current_user(request)).email
        except HTTPException:
            pass
    return actor_of(request)


async def _run_for_org(db: AsyncSession, run_id: str, org: str) -> SandboxRun:
    run = (
        await db.execute(
            select(SandboxRun).where(
                SandboxRun.id == run_id,
                SandboxRun.org_id == org,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Sandbox run not found")
    return run


async def _mark_timed_out(
    run: SandboxRun,
    db: AsyncSession,
    org: str,
    now: datetime,
) -> None:
    if run.status != "running":
        return
    run.status = "timed_out"
    run.completed_at = now
    run.error = "Execution deadline elapsed"
    await audit.log(
        db,
        "system",
        "timed_out",
        "sandbox_run",
        run.id,
        {
            "gateway_id": run.gateway_id,
            "tool_calls": run.tool_calls,
            "error": run.error,
        },
        org=org,
    )


async def _expire_stale_runs(
    db: AsyncSession, org: str, now: datetime
) -> tuple[list[SandboxRun], int]:
    rows = (
        (
            await db.execute(
                select(SandboxRun).where(
                    SandboxRun.org_id == org,
                    SandboxRun.status == "running",
                )
            )
        )
        .scalars()
        .all()
    )
    active: list[SandboxRun] = []
    expired = 0
    for run in rows:
        if now >= _deadline(run):
            await _mark_timed_out(run, db, org, now)
            expired += 1
        else:
            active.append(run)
    return active, expired


@router.post("", response_model=RunResponse, status_code=201)
async def create_run(
    body: RunCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    limits = _limits()
    if body.source_bytes > limits.max_source_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Source exceeds {limits.max_source_bytes} byte limit",
        )
    gateway = await get_scoped(db, Gateway, body.gateway_id, org)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")

    now = datetime.now(timezone.utc)
    active, expired = await _expire_stale_runs(db, org, now)
    if expired:
        await db.commit()
    if len(active) >= limits.max_active_runs:
        raise HTTPException(
            status_code=429,
            detail=f"At most {limits.max_active_runs} Sandbox runs may be active",
        )

    run = SandboxRun(
        id=str(uuid.uuid4()),
        org_id=org,
        actor=await _actor(request),
        gateway_id=gateway.id,
        language=body.language,
        source_digest=body.source_digest,
        source_bytes=body.source_bytes,
        status="running",
        timeout_ms=limits.timeout_ms,
        max_tool_calls=limits.max_tool_calls,
        max_output_bytes=limits.max_output_bytes,
        max_tool_payload_bytes=limits.max_tool_payload_bytes,
        started_at=now,
    )
    db.add(run)
    await db.flush()
    await audit.log(
        db,
        run.actor,
        "start",
        "sandbox_run",
        run.id,
        {
            "gateway_id": run.gateway_id,
            "language": run.language,
            "source_digest": run.source_digest,
            "source_bytes": run.source_bytes,
            "timeout_ms": run.timeout_ms,
            "max_tool_calls": run.max_tool_calls,
        },
        org=org,
    )
    await db.commit()
    await db.refresh(run)
    return _response(run)


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    run = await _run_for_org(db, run_id, org)
    now = datetime.now(timezone.utc)
    if run.status == "running" and now >= _deadline(run):
        await _mark_timed_out(run, db, org, now)
        await db.commit()
        await db.refresh(run)
    return _response(run)


@router.post("/{run_id}/tools/{tool_name}")
async def execute_tool(
    run_id: str,
    tool_name: str,
    payload: dict[str, Any],
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    if not _TOOL_NAME_RE.fullmatch(tool_name):
        raise HTTPException(status_code=422, detail="Invalid tool name")

    encoded = json.dumps(payload, separators=(",", ":"), default=str).encode()
    run = await _run_for_org(db, run_id, org)
    if len(encoded) > run.max_tool_payload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Tool payload exceeds {run.max_tool_payload_bytes} byte limit",
        )

    now = datetime.now(timezone.utc)
    if run.status != "running":
        raise HTTPException(status_code=409, detail=f"Sandbox run is {run.status}")
    if now >= _deadline(run):
        await _mark_timed_out(run, db, org, now)
        await db.commit()
        raise HTTPException(status_code=410, detail="Sandbox run timed out")

    gateway = await get_scoped(db, Gateway, run.gateway_id, org)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")

    max_tool_calls = run.max_tool_calls
    step = (
        await db.execute(
            update(SandboxRun)
            .where(
                SandboxRun.id == run.id,
                SandboxRun.org_id == org,
                SandboxRun.status == "running",
                SandboxRun.tool_calls < SandboxRun.max_tool_calls,
            )
            .values(tool_calls=SandboxRun.tool_calls + 1)
            .returning(SandboxRun.tool_calls)
        )
    ).scalar_one_or_none()
    if step is None:
        await db.rollback()
        raise HTTPException(
            status_code=429,
            detail=f"Sandbox run is limited to {max_tool_calls} tool calls",
        )
    await db.commit()

    remaining = max(0.25, (_deadline(run) - now).total_seconds())
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Id": "sandbox-code",
        "X-Session-Id": f"sandbox-code:{run.id}",
        "X-Plan": "Sandbox code execution",
        "X-Step": f"{step}/{max_tool_calls}",
    }
    try:
        async with httpx.AsyncClient(timeout=min(15.0, remaining)) as client:
            response = await client.post(
                f"{gateway.endpoint.rstrip('/')}/tool/{tool_name}",
                content=encoded,
                headers=headers,
            )
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach gateway at {gateway.endpoint}",
        ) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Gateway tool call timed out") from exc

    response_headers = {}
    content_type = response.headers.get("content-type")
    if content_type:
        response_headers["content-type"] = content_type
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=response_headers,
    )


async def _finish_run(
    run: SandboxRun,
    body: RunComplete,
    request: Request,
    db: AsyncSession,
    org: str,
) -> SandboxRun:
    if run.status in _FINAL_STATUSES:
        return run

    run.status = body.status
    run.completed_at = datetime.now(timezone.utc)
    run.output_bytes = min(body.output_bytes, run.max_output_bytes)
    run.error = body.error
    await audit.log(
        db,
        await _actor(request),
        body.status,
        "sandbox_run",
        run.id,
        {
            "gateway_id": run.gateway_id,
            "duration_ms": body.duration_ms,
            "tool_calls": run.tool_calls,
            "output_bytes": run.output_bytes,
            "error": run.error,
        },
        org=org,
    )
    await db.commit()
    await db.refresh(run)
    return run


@router.post("/{run_id}/complete", response_model=RunResponse)
async def complete_run(
    run_id: str,
    body: RunComplete,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    run = await _run_for_org(db, run_id, org)
    return _response(await _finish_run(run, body, request, db, org))


@router.delete("/{run_id}", response_model=RunResponse)
async def cancel_run(
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    run = await _run_for_org(db, run_id, org)
    elapsed = max(
        0,
        int((datetime.now(timezone.utc) - _as_utc(run.started_at)).total_seconds() * 1000),
    )
    body = RunComplete(
        status="cancelled",
        duration_ms=elapsed,
        output_bytes=run.output_bytes,
        error="Cancelled by operator",
    )
    return _response(await _finish_run(run, body, request, db, org))
