"""Gateway management API."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.auth.dependencies import get_current_org
from control_plane.database import async_session, get_db
from control_plane.models.database import Gateway, Tool
from control_plane.models.schemas import GatewayCreate, GatewayResponse, GatewayUpdate
from control_plane.models.scoping import get_scoped, scoped, stamp
from control_plane.services.audit_service import actor_of, audit
from control_plane.services.push_service import PushService, gateway_config_headers

log = logging.getLogger("control_plane.gateways")

router = APIRouter(prefix="/api/gateways", tags=["gateways"])
push_service = PushService()

# In-memory config queue for offline gateways
config_queue: dict[str, list[dict[str, Any]]] = {}

# Health check background task handle
_health_check_task: asyncio.Task | None = None
HEARTBEAT_TIMEOUT_SECONDS = 90


async def _health_check_loop() -> None:
    """Background loop: mark gateways unhealthy if heartbeat > 90s ago."""
    while True:
        await asyncio.sleep(15)
        try:
            async with async_session() as db:
                result = await db.execute(select(Gateway))
                gateways = result.scalars().all()
                now = datetime.now(timezone.utc)
                for gw in gateways:
                    if gw.status == "healthy" and gw.last_heartbeat:
                        # SQLite returns naive datetimes even for timezone=True
                        # columns; normalize to UTC before comparing with `now`.
                        last_hb = gw.last_heartbeat
                        if last_hb.tzinfo is None:
                            last_hb = last_hb.replace(tzinfo=timezone.utc)
                        delta = (now - last_hb).total_seconds()
                        if delta > HEARTBEAT_TIMEOUT_SECONDS:
                            gw.status = "unhealthy"
                            log.info(f"Gateway {gw.id} marked unhealthy (last heartbeat {delta:.0f}s ago)")
                await db.commit()
        except Exception as e:
            log.warning(f"Health check loop error: {e}")


def start_health_check() -> None:
    """Start the background health-check task (call once at app startup)."""
    global _health_check_task
    if _health_check_task is None or _health_check_task.done():
        _health_check_task = asyncio.create_task(_health_check_loop())


def stop_health_check() -> None:
    """Cancel the background health-check task."""
    global _health_check_task
    if _health_check_task and not _health_check_task.done():
        _health_check_task.cancel()
        _health_check_task = None


def _to_response(gateway: Gateway, tools_count: int = 0) -> GatewayResponse:
    """Build a GatewayResponse, surfacing the stored enforcement mode."""
    return GatewayResponse(
        id=gateway.id, name=gateway.name, endpoint=gateway.endpoint,
        description=gateway.description, status=gateway.status,
        last_heartbeat=gateway.last_heartbeat, tools_count=tools_count,
        mode=(gateway.config or {}).get("mode", "enforce"),
        created_at=gateway.created_at, updated_at=gateway.updated_at,
    )


@router.get("", response_model=list[GatewayResponse])
async def list_gateways(db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    result = await db.execute(scoped(select(Gateway), Gateway, org))
    gateways = result.scalars().all()
    responses = []
    for s in gateways:
        tools_result = await db.execute(scoped(select(Tool).where(Tool.gateway_id == s.id), Tool, org))
        tools_count = len(tools_result.scalars().all())
        responses.append(_to_response(s, tools_count))
    return responses


@router.post("", response_model=GatewayResponse)
async def register_gateway(body: GatewayCreate, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    existing = await db.get(Gateway, body.id)
    if existing:
        raise HTTPException(status_code=409, detail=f"Gateway {body.id} already exists")
    gateway = Gateway(id=body.id, name=body.name, endpoint=body.endpoint, description=body.description)
    stamp(gateway, org)
    db.add(gateway)
    await audit.log(db, actor_of(request), "create", "gateway", body.id, {"name": body.name, "endpoint": body.endpoint}, org=org)
    await db.commit()
    await db.refresh(gateway)
    return _to_response(gateway, 0)


@router.get("/{gateway_id}", response_model=GatewayResponse)
async def get_gateway(gateway_id: str, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")
    tools_result = await db.execute(scoped(select(Tool).where(Tool.gateway_id == gateway_id), Tool, org))
    tools_count = len(tools_result.scalars().all())
    return _to_response(gateway, tools_count)


@router.patch("/{gateway_id}", response_model=GatewayResponse)
async def update_gateway(gateway_id: str, body: GatewayUpdate, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(gateway, field, value)
    await audit.log(db, actor_of(request), "update", "gateway", gateway_id, changes, org=org)
    await db.commit()
    await db.refresh(gateway)
    return await get_gateway(gateway_id, db, org)


@router.delete("/{gateway_id}")
async def delete_gateway(gateway_id: str, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")
    await audit.log(db, actor_of(request), "delete", "gateway", gateway_id, {"name": gateway.name}, org=org)
    await db.delete(gateway)
    await db.commit()
    return {"deleted": gateway_id}


@router.put("/{gateway_id}/mode", response_model=GatewayResponse)
async def set_mode(gateway_id: str, body: dict, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Set a gateway's enforcement mode (enforce | shadow) and push it live.

    The mode is persisted in the gateway's config so it survives restarts and
    is re-applied on every subsequent push. If the gateway is reachable, the
    new mode is pushed immediately; if not, it takes effect on the next push.
    """
    mode = body.get("mode")
    if mode not in ("enforce", "shadow"):
        raise HTTPException(status_code=400, detail="mode must be 'enforce' or 'shadow'")

    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    # Persist mode in the gateway config (JSON column reassigned so SQLAlchemy
    # detects the change).
    gateway.config = {**(gateway.config or {}), "mode": mode}
    await audit.log(db, actor_of(request), "set_mode", "gateway", gateway_id, {"mode": mode}, org=org)
    await db.commit()
    await db.refresh(gateway)

    # Best-effort live push so the change is immediate; ignore transport errors.
    try:
        await push_service.push_to_gateway(db, gateway_id)
    except Exception as exc:  # noqa: BLE001 — push is best-effort
        log.warning("mode set but live push failed for %s: %s", gateway_id, exc)

    return _to_response(gateway, 0)


@router.post("/{gateway_id}/push")
async def push_config(gateway_id: str, request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Push current config to a specific gateway."""
    if await get_scoped(db, Gateway, gateway_id, org) is None:
        raise HTTPException(status_code=404, detail="Gateway not found")
    result = await push_service.push_to_gateway(db, gateway_id)
    await audit.log(db, actor_of(request), "push", "gateway", gateway_id, {"status": result.status}, org=org)
    await db.commit()
    if result.status == "error":
        raise HTTPException(status_code=502, detail=result.message)
    return result


@router.post("/push-all")
async def push_all(request: Request, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Push config to all registered gateways in the caller's org."""
    result = await push_service.push_to_all(db, org=org)
    await audit.log(db, actor_of(request), "push_all", "gateway", "*", {"succeeded": result.succeeded, "failed": result.failed}, org=org)
    await db.commit()
    return result


@router.get("/{gateway_id}/health")
async def check_health(gateway_id: str, db: AsyncSession = Depends(get_db), org: str = Depends(get_current_org)):
    """Check health of a gateway by calling its /health endpoint."""
    gateway = await get_scoped(db, Gateway, gateway_id, org)
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


# ─── Gateway Lifecycle Endpoints ─────────────────────────────────────────


@router.post("/{gateway_id}/register")
async def gateway_register(
    gateway_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Gateway calls this on startup. Marks healthy, returns full config bundle.

    Self-registration: a gateway that boots before it has been provisioned in the
    control plane is auto-created here (rather than 404'd). This removes the
    provisioning-order dependency — a fresh gateway becomes governed the moment it
    starts, and the demo stack / restarts don't require a separate create step.
    """
    # The gateway advertises the URL the control plane should push config to.
    # This MUST include the port — the caller's source host alone (from
    # request.client) drops it, breaking config pushes (config goes to
    # http://host/config with no port → connection refused).
    callback_url = ""
    reg_org = "default"
    try:
        body = await request.json()
        if isinstance(body, dict):
            callback_url = (body.get("callback_url") or "").strip()
            # A gateway declares its org at registration (no user token on this
            # path). Absent that, it lands in the default org.
            reg_org = (body.get("org_id") or "default").strip() or "default"
    except Exception:
        pass

    def _fallback_endpoint() -> str:
        """Best-effort endpoint from the caller's host (no port — last resort)."""
        client_host = request.client.host if request.client else ""
        return f"http://{client_host}" if client_host else ""

    gateway = await db.get(Gateway, gateway_id)
    created = False
    if not gateway:
        # Auto-provision on first contact. Prefer the advertised callback URL
        # (has the port); fall back to the caller's host if none was sent.
        gateway = Gateway(
            id=gateway_id,
            name=gateway_id,
            endpoint=callback_url or _fallback_endpoint(),
            description="Auto-registered on gateway startup",
            org_id=reg_org,
        )
        db.add(gateway)
        created = True
    elif callback_url and gateway.endpoint != callback_url:
        # Keep the endpoint current — a gateway may restart on a new port, or an
        # earlier portless auto-register needs correcting so pushes can reach it.
        gateway.endpoint = callback_url

    gateway.status = "healthy"
    gateway.last_heartbeat = datetime.now(timezone.utc)
    if created:
        await audit.log(
            db, actor_of(request), "auto-register", "gateway", gateway_id,
            {"endpoint": gateway.endpoint}, org=gateway.org_id or "default",
        )
    await db.commit()

    # Build and return the full config bundle
    bundle = await push_service._build_config(db, gateway)

    # Include quotas and agent_auth from gateway config if stored
    bundle.setdefault("quotas", gateway.config.get("quotas", {}))
    bundle.setdefault("agent_auth", gateway.config.get("agent_auth", {}))

    # Include persisted A2A agents so the gateway reconnects them on startup.
    from control_plane.routers.a2a_agents import build_a2a_config
    a2a = await build_a2a_config(db, gateway_id, gateway.org_id)
    if a2a:
        bundle["a2a_agents"] = a2a

    # Drain any queued config. This goes in a SIBLING key, not inside the bundle:
    # the gateway applies `config` as one document, so a nested key was silently
    # dropped (it was "queued_updates", which nothing ever read). Naming it
    # config_updates matches the heartbeat path, which the gateway does apply.
    queued = config_queue.pop(gateway_id, [])

    log.info(f"Gateway {gateway_id} registered (healthy)")
    response: dict[str, Any] = {"status": "registered", "config": bundle}
    if queued:
        response["config_updates"] = queued
    return response


@router.post("/{gateway_id}/heartbeat")
async def gateway_heartbeat(gateway_id: str, db: AsyncSession = Depends(get_db)):
    """Gateway heartbeat every 30s. Returns pending config changes if any."""
    gateway = await db.get(Gateway, gateway_id)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    was_unhealthy = gateway.status != "healthy"
    gateway.status = "healthy"
    gateway.last_heartbeat = datetime.now(timezone.utc)
    await db.commit()

    response: dict[str, Any] = {"status": "ok"}

    # If reconnecting from unhealthy state, send full bundle
    if was_unhealthy:
        bundle = await push_service._build_config(db, gateway)
        bundle.setdefault("quotas", gateway.config.get("quotas", {}))
        bundle.setdefault("agent_auth", gateway.config.get("agent_auth", {}))
        response["config"] = bundle
        response["reason"] = "reconnect"
        log.info(f"Gateway {gateway_id} reconnected — sending full config")

    # Drain queued config. Pool availability is included on every heartbeat so
    # all gateways in an org converge after another gateway depletes or funds a
    # pool; the reporting gateway also receives the same state immediately in
    # the cost-ingestion response.
    queued = config_queue.pop(gateway_id, [])
    from control_plane.routers.broker_pilot import pool_snapshot

    queued.append(
        {
            "broker_pools": await pool_snapshot(
                db, gateway.org_id or "default"
            )
        }
    )
    response["config_updates"] = queued

    return response


@router.get("/{gateway_id}/config-bundle")
async def get_config_bundle(
    gateway_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Returns full current config for a gateway."""
    if getattr(request.state, "machine_authenticated", False):
        gateway = await db.get(Gateway, gateway_id)
    else:
        gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    bundle = await push_service._build_config(db, gateway)
    bundle.setdefault("quotas", gateway.config.get("quotas", {}))
    bundle.setdefault("agent_auth", gateway.config.get("agent_auth", {}))
    return bundle


@router.post("/{gateway_id}/push-config")
async def push_config_lifecycle(
    gateway_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    org: str = Depends(get_current_org),
):
    """Operator pushes config NOW. If healthy -> forward immediately. If unhealthy -> queue."""
    gateway = await get_scoped(db, Gateway, gateway_id, org)
    if not gateway:
        raise HTTPException(status_code=404, detail="Gateway not found")

    body = await request.json()

    if gateway.status == "healthy":
        # Forward immediately via the existing push mechanism
        async with httpx.AsyncClient(
            timeout=10.0, headers=gateway_config_headers()
        ) as client:
            try:
                resp = await client.post(
                    f"{gateway.endpoint}/config", json=body
                )
                if resp.status_code == 200:
                    return {"status": "applied", "gateway_id": gateway_id}
                else:
                    return {"status": "error", "gateway_id": gateway_id, "detail": resp.text[:200]}
            except (httpx.ConnectError, httpx.TimeoutException):
                # Gateway became unreachable — queue instead
                gateway.status = "unhealthy"
                await db.commit()
                config_queue.setdefault(gateway_id, []).append(body)
                return {"status": "queued", "gateway_id": gateway_id, "reason": "became_unreachable"}
    else:
        # Queue for later delivery on heartbeat
        config_queue.setdefault(gateway_id, []).append(body)
        return {"status": "queued", "gateway_id": gateway_id, "reason": "gateway_offline"}


@router.get("/{gateway_id}/spend")
async def get_gateway_spend(gateway_id: str, db: AsyncSession = Depends(get_db)):
    """Return the gateway's durable per-agent spend snapshot."""
    gateway = await db.get(Gateway, gateway_id)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")
    return {"spend": (gateway.config or {}).get("agent_spend", {})}


@router.post("/{gateway_id}/spend")
async def set_gateway_spend(
    gateway_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    """Persist the gateway's per-agent spend snapshot."""
    gateway = await db.get(Gateway, gateway_id)
    if gateway is None:
        raise HTTPException(status_code=404, detail="Gateway not found")
    body = await request.json()
    spend = body.get("spend", {}) if isinstance(body, dict) else {}
    if not isinstance(spend, dict):
        raise HTTPException(status_code=422, detail="spend must be an object")
    existing = (gateway.config or {}).get("agent_spend", {})
    reset = bool(body.get("reset")) if isinstance(body, dict) else False
    if reset:
        merged = {
            agent_id: max(0.0, float(amount or 0.0))
            for agent_id, amount in spend.items()
        }
    else:
        merged = {
            agent_id: max(
                float(existing.get(agent_id, 0.0) or 0.0),
                float(amount or 0.0),
            )
            for agent_id, amount in {**existing, **spend}.items()
        }

    config = {**(gateway.config or {}), "agent_spend": merged}
    if reset:
        reset_at = body.get("reset_at") or datetime.now(timezone.utc).isoformat()
        budget_reset = {
            **config.get("budget_reset", {}),
            "last_reset_at": reset_at,
        }
        config["budget_reset"] = budget_reset
    gateway.config = config
    await db.commit()
    return {"status": "stored", "agents": len(merged), "reset": reset}
