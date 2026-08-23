"""FastAPI application for the control plane."""

from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from control_plane.auth.middleware import AuthMiddleware
from control_plane.auth.router import router as auth_router
from control_plane.auth.sso_router import router as sso_router
from control_plane.database import async_session, engine
from control_plane.env import (
    control_plane_replicas,
    is_production,
    validate_production_posture,
)
from control_plane.models.database import Base
from control_plane.routers import (
    a2a_agents,
    agent_routing,
    agents,
    approvals,
    audit,
    broker_pilot,
    compliance,
    costs,
    discovery,
    experiments,
    gateways,
    mcp_servers,
    metering,
    model_config,
    payments,
    policies,
    provider_routes,
    providers,
    proxy,
    quotas,
    roi,
    routing_controls,
    sandbox,
    token_broker,
    tools,
    traces,
    trust,
)
from ostiari.http_limits import BodySizeLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_production_posture()

    import control_plane.auth.models  # noqa: F401 — register auth tables
    from control_plane.persistence import (
        import_legacy_state,
        load_runtime_caches,
        load_state,
        start_runtime_cache_sync,
        stop_runtime_cache_sync,
    )
    from control_plane.routers.gateways import start_health_check, stop_health_check

    if not is_production():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    from control_plane.routers.approvals import load_approval_cache
    from control_plane.routers.traces import (
        load_recent_trace_cache,
        start_trace_bus,
        stop_trace_bus,
    )

    async with async_session() as db:
        await import_legacy_state(db, load_state())
        await load_runtime_caches(db)
        await load_approval_cache(db)
        await load_recent_trace_cache(db)

    # Seed demo data so the dashboard isn't empty (skip in clean-install mode).
    # Production posture requires no-demo mode. Seeders remain idempotent for
    # local development and tests.
    import os
    if os.environ.get("OSTIARI_NO_DEMO", "").lower() not in ("1", "true", "yes"):
        from control_plane.demo_seed import (
            seed_demo_agents,
            seed_demo_approvals,
            seed_demo_broker_pools,
            seed_demo_db,
            seed_demo_experiments,
            seed_demo_pricing,
            seed_demo_quotas,
        )
        from control_plane.routers.traces import persist_demo_traces, seed_traces
        seed_traces()
        seed_demo_agents()
        seed_demo_approvals()
        seed_demo_experiments()
        seed_demo_pricing()
        async with async_session() as db:
            await seed_demo_db(db)
            await persist_demo_traces(db)
            # Both of these read the usage records seed_demo_db writes — quota
            # spend and broker pool draw-down are aggregates over them — so on a
            # first run they have to exist first.
            await seed_demo_quotas(db)
            await seed_demo_broker_pools(db)

    # Start background health-check loop for gateways
    start_runtime_cache_sync()
    start_trace_bus()
    start_health_check()

    yield

    # Stop health-check loop
    await stop_health_check()
    await stop_trace_bus()
    await stop_runtime_cache_sync()

    from control_plane.redis_client import close_redis

    await close_redis()
    await engine.dispose()


app = FastAPI(
    title="Ostiari Control Plane",
    description="Centralized management for Ostiari gateway fleet",
    version="0.1.0",
    lifespan=lifespan,
)

# Coarse API authentication gate (no-op unless OSTIARI_REQUIRE_AUTH is set).
# Added before CORS so it runs after CORS in the response path.
app.add_middleware(AuthMiddleware)

# Reject oversized request bodies (DoS guard) before they are buffered.
app.add_middleware(BodySizeLimitMiddleware)

def _cors_config() -> dict:
    """CORS settings. Wildcard-with-credentials is unsafe, so if specific origins
    are set via OSTIARI_CORS_ORIGINS (comma-separated) we use them WITH
    credentials; otherwise we allow all origins but WITHOUT credentials (the
    browser-safe combination). Production should set explicit origins."""
    import os
    raw = os.environ.get("OSTIARI_CORS_ORIGINS", "").strip()
    if raw:
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        return {"allow_origins": origins, "allow_credentials": True}
    return {"allow_origins": ["*"], "allow_credentials": False}


app.add_middleware(
    CORSMiddleware,
    allow_methods=["*"],
    allow_headers=["*"],
    **_cors_config(),
)

app.include_router(auth_router)
app.include_router(sso_router)
app.include_router(gateways.router)
app.include_router(agents.router)
app.include_router(agent_routing.router)
app.include_router(tools.router)
app.include_router(policies.router)
app.include_router(mcp_servers.router)
app.include_router(costs.router)
app.include_router(discovery.router)
app.include_router(approvals.router)
app.include_router(experiments.router)
app.include_router(model_config.router)
app.include_router(routing_controls.router)
app.include_router(providers.router)
app.include_router(provider_routes.router)
app.include_router(quotas.router)
app.include_router(proxy.router)
app.include_router(sandbox.router)
app.include_router(traces.router)
app.include_router(audit.router)
app.include_router(compliance.router)
app.include_router(metering.router)
app.include_router(trust.router)
app.include_router(payments.router)
app.include_router(roi.router)
app.include_router(token_broker.router)
app.include_router(broker_pilot.router)
app.include_router(a2a_agents.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "control-plane"}


@app.get("/api/ready")
async def ready():
    try:
        async with async_session() as db:
            await db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - readiness must report dependency failure
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": "control-plane",
                "database": "unavailable",
                "detail": str(exc),
            },
        )
    dependencies: dict[str, str] = {
        "status": "ready",
        "service": "control-plane",
        "database": "available",
    }
    if is_production() or control_plane_replicas() > 1:
        from control_plane.persistence import runtime_cache_sync_error
        from control_plane.redis_client import get_redis
        from control_plane.routers.traces import trace_bus_error

        try:
            redis = await get_redis()
            if redis is None or not await cast("Any", redis).ping():
                raise RuntimeError("Redis unavailable")
            sync_error = runtime_cache_sync_error()
            bus_error = trace_bus_error()
            if sync_error:
                raise RuntimeError(f"runtime synchronization failed: {sync_error}")
            if bus_error:
                raise RuntimeError(f"trace fan-out failed: {bus_error}")
        except Exception as exc:  # noqa: BLE001 - readiness must fail closed
            return JSONResponse(
                status_code=503,
                content={
                    **dependencies,
                    "status": "not_ready",
                    "redis": "unavailable",
                    "detail": str(exc),
                },
            )
        dependencies["redis"] = "available"
    return dependencies
