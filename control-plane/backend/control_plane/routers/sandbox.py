"""Browser-isolated Sandbox run lifecycle and governed tool bridge."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from jwt.exceptions import PyJWTError as JWTError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org, get_current_user
from control_plane.database import get_db
from control_plane.models.database import Gateway, SandboxRun
from control_plane.models.scoping import get_scoped
from control_plane.services.audit_service import actor_of, audit
from control_plane.services.gateway_credentials import gateway_agent_credential

router = APIRouter(prefix="/api/sandbox/runs", tags=["sandbox"])

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_FINAL_STATUSES = {"completed", "error", "cancelled", "timed_out"}
_GATEWAY_CHUNK_BYTES = 16 * 1024


class _GatewayResponseTooLargeError(Exception):
    pass


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


async def _gateway_credential(request: Request) -> tuple[str, str | None]:
    agent_id, authorization = gateway_agent_credential(
        token_env="OSTIARI_SANDBOX_GATEWAY_TOKEN",
        agent_env="OSTIARI_SANDBOX_GATEWAY_AGENT_ID",
        default_agent_id="sandbox-code",
    )
    if authorization:
        return agent_id, authorization

    caller_authorization = request.headers.get("Authorization", "")
    if not caller_authorization.startswith("Bearer "):
        return "sandbox-code", None

    try:
        principal = await get_current_user(request)
        # The same token was just signature-validated by get_current_user. Decode
        # its claims to mirror the gateway's agent-id precedence exactly.
        claims = jwt.decode(
            caller_authorization.removeprefix("Bearer "),
            options={"verify_signature": False},
        )
    except (HTTPException, JWTError):
        return "sandbox-code", None

    agent_id = str(
        claims.get("agent_id")
        or claims.get("custom:agent_id")
        or claims.get("client_id")
        or claims.get("sub")
        or principal.subject
        or "sandbox-code"
    )
    return agent_id, caller_authorization


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


async def _claim_terminal(
    run: SandboxRun,
    db: AsyncSession,
    org: str,
    now: datetime,
    *,
    actor: str,
    status: Literal["completed", "error", "cancelled", "timed_out"],
    output_bytes: int,
    error: str,
    duration_ms: int | None = None,
) -> bool:
    claimed = (
        await db.execute(
            update(SandboxRun)
            .where(
                SandboxRun.id == run.id,
                SandboxRun.org_id == org,
                SandboxRun.status == "running",
            )
            .values(
                status=status,
                active_slot=None,
                completed_at=now,
                output_bytes=min(output_bytes, run.max_output_bytes),
                error=error,
            )
            .returning(SandboxRun.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    await db.refresh(run)
    if claimed is None:
        return False

    details = {
        "gateway_id": run.gateway_id,
        "tool_calls": run.tool_calls,
        "output_bytes": run.output_bytes,
        "error": run.error,
    }
    if duration_ms is not None:
        details["duration_ms"] = duration_ms
    await audit.log(
        db,
        actor,
        status,
        "sandbox_run",
        run.id,
        details,
        org=org,
    )
    return True


async def _mark_timed_out(
    run: SandboxRun,
    db: AsyncSession,
    org: str,
    now: datetime,
) -> bool:
    return await _claim_terminal(
        run,
        db,
        org,
        now,
        actor="system",
        status="timed_out",
        output_bytes=run.output_bytes,
        error="Execution deadline elapsed",
    )


async def _expire_stale_runs(
    db: AsyncSession, org: str, now: datetime
) -> int:
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
    expired = 0
    for run in rows:
        if now >= _deadline(run):
            expired += int(await _mark_timed_out(run, db, org, now))
    return expired


async def _reserve_run(
    db: AsyncSession,
    values: dict[str, Any],
    max_active_runs: int,
) -> SandboxRun | None:
    incompatible = (
        await db.execute(
            select(SandboxRun.id)
            .where(
                SandboxRun.org_id == values["org_id"],
                SandboxRun.status == "running",
                or_(
                    SandboxRun.active_slot.is_(None),
                    SandboxRun.active_slot >= max_active_runs,
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if incompatible is not None:
        return None

    dialect = db.get_bind().dialect.name
    insert_fn = postgresql_insert if dialect == "postgresql" else sqlite_insert
    for slot in range(max_active_runs):
        run_id = (
            await db.execute(
                insert_fn(SandboxRun)
                .values(**values, active_slot=slot)
                .on_conflict_do_nothing(
                    index_elements=[SandboxRun.org_id, SandboxRun.active_slot]
                )
                .returning(SandboxRun.id)
            )
        ).scalar_one_or_none()
        if run_id is not None:
            run = await db.get(SandboxRun, run_id)
            if run is None:  # pragma: no cover - defensive against a broken driver
                raise RuntimeError("Inserted Sandbox run could not be reloaded")
            return run
    return None


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
    expired = await _expire_stale_runs(db, org, now)
    run = await _reserve_run(
        db,
        {
            "id": str(uuid.uuid4()),
            "org_id": org,
            "actor": await _actor(request),
            "gateway_id": gateway.id,
            "language": body.language,
            "source_digest": body.source_digest,
            "source_bytes": body.source_bytes,
            "status": "running",
            "timeout_ms": limits.timeout_ms,
            "max_tool_calls": limits.max_tool_calls,
            "max_output_bytes": limits.max_output_bytes,
            "max_tool_payload_bytes": limits.max_tool_payload_bytes,
            "tool_calls": 0,
            "output_bytes": 0,
            "error": "",
            "started_at": now,
            "completed_at": None,
        },
        limits.max_active_runs,
    )
    if run is None:
        if expired:
            await db.commit()
        raise HTTPException(
            status_code=429,
            detail=f"At most {limits.max_active_runs} Sandbox runs may be active",
        )

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


async def _gateway_headers(
    request: Request,
    run: SandboxRun,
    step: int,
    max_tool_calls: int,
) -> dict[str, str]:
    agent_id, authorization = await _gateway_credential(request)
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Id": agent_id,
        "X-Session-Id": f"sandbox-code:{run.id}",
        "X-Plan": "Sandbox code execution",
        "X-Step": f"{step}/{max_tool_calls}",
    }
    if authorization:
        headers["Authorization"] = authorization
    return headers


async def _stream_gateway_response(
    url: str,
    encoded: bytes,
    headers: dict[str, str],
    deadline: datetime,
    max_response_bytes: int,
) -> tuple[int, dict[str, str], bytes]:
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise TimeoutError

    content = bytearray()
    response_headers: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=min(15.0, remaining)) as client:
        async with asyncio.timeout(remaining):
            async with client.stream(
                "POST",
                url,
                content=encoded,
                headers=headers,
            ) as response:
                content_type = response.headers.get("content-type")
                if content_type:
                    response_headers["content-type"] = content_type
                async for chunk in response.aiter_bytes(chunk_size=_GATEWAY_CHUNK_BYTES):
                    if len(content) + len(chunk) > max_response_bytes:
                        raise _GatewayResponseTooLargeError
                    content.extend(chunk)
                return response.status_code, response_headers, bytes(content)


@router.post("/{run_id}/tools/{tool_name}")
async def execute_tool(
    run_id: str,
    tool_name: str,
    payload: dict[str, Any],
    request: Request,
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
        if run.status == "timed_out":
            raise HTTPException(status_code=410, detail="Sandbox run timed out")
        raise HTTPException(status_code=409, detail=f"Sandbox run is {run.status}")

    gateway = await get_scoped(db, Gateway, run.gateway_id, org)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")

    max_tool_calls = run.max_tool_calls
    current_run_id = run.id
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
        run = await _run_for_org(db, current_run_id, org)
        if run.status != "running":
            raise HTTPException(status_code=409, detail=f"Sandbox run is {run.status}")
        raise HTTPException(
            status_code=429,
            detail=f"Sandbox run is limited to {max_tool_calls} tool calls",
        )
    await db.commit()

    deadline = _deadline(run)
    headers = await _gateway_headers(request, run, step, max_tool_calls)
    try:
        status_code, response_headers, content = await _stream_gateway_response(
            f"{gateway.endpoint.rstrip('/')}/tool/{tool_name}",
            encoded,
            headers,
            deadline,
            run.max_output_bytes,
        )
    except _GatewayResponseTooLargeError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Gateway response exceeds {run.max_output_bytes} byte limit",
        ) from exc
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Cannot reach gateway at {gateway.endpoint}",
        ) from exc
    except (TimeoutError, httpx.TimeoutException) as exc:
        timeout_now = datetime.now(timezone.utc)
        if timeout_now >= deadline:
            await _mark_timed_out(run, db, org, timeout_now)
            await db.commit()
        raise HTTPException(status_code=504, detail="Gateway tool call timed out") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Gateway tool call failed") from exc

    if status_code == 401:
        return Response(
            content=json.dumps(
                {
                    "detail": "Gateway rejected Sandbox credentials",
                    "gateway_status": 401,
                }
            ),
            status_code=502,
            media_type="application/json",
        )
    return Response(
        content=content,
        status_code=status_code,
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

    now = datetime.now(timezone.utc)
    if now >= _deadline(run):
        await _mark_timed_out(run, db, org, now)
        await db.commit()
        return run

    await _claim_terminal(
        run,
        db,
        org,
        now,
        actor=await _actor(request),
        status=body.status,
        output_bytes=body.output_bytes,
        error=body.error,
        duration_ms=body.duration_ms,
    )
    await db.commit()
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
    now = datetime.now(timezone.utc)
    if run.status in _FINAL_STATUSES:
        return _response(run)
    if now >= _deadline(run):
        await _mark_timed_out(run, db, org, now)
        await db.commit()
        return _response(run)

    elapsed = max(
        0,
        min(
            300_000,
            int((now - _as_utc(run.started_at)).total_seconds() * 1000),
        ),
    )
    body = RunComplete(
        status="cancelled",
        duration_ms=elapsed,
        output_bytes=run.output_bytes,
        error="Cancelled by operator",
    )
    return _response(await _finish_run(run, body, request, db, org))
