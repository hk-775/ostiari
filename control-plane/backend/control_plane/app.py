"""FastAPI application for the control plane."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from control_plane.auth.router import router as auth_router
from control_plane.auth.sso_router import router as sso_router
from control_plane.database import engine
from control_plane.models.database import Base
from control_plane.routers import (
    a2a_agents,
    agents,
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
    providers,
    proxy,
    quotas,
    roi,
    token_broker,
    tools,
    traces,
    trust,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import control_plane.auth.models  # noqa: F401 — register auth tables
    from control_plane.persistence import load_state, save_state
    from control_plane.routers.gateways import start_health_check, stop_health_check

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Restore in-memory state from previous session
    state = load_state()
    if "quotas" in state:
        from control_plane.routers.quotas import QuotaResponse, _next_id, _quotas
        for q in state["quotas"]:
            _quotas[q["id"]] = QuotaResponse(**q)
        if state["quotas"]:
            _next_id[0] = max(q["id"] for q in state["quotas"]) + 1

    if "experiments" in state:
        from control_plane.routers.experiments import ExperimentResponse, _experiments
        for e in state["experiments"]:
            _experiments[e["name"]] = ExperimentResponse(**e)

    if "models" in state:
        from control_plane.routers.model_config import ModelConfig, _models
        for m in state["models"]:
            _models[m["name"]] = ModelConfig(**m)

    if "providers" in state:
        from control_plane.routers.providers import _ProviderRecord, _providers
        for p in state["providers"]:
            _providers[p["name"]] = _ProviderRecord(**p)

    if "roi_cost_model" in state and state["roi_cost_model"]:
        from control_plane.routers.roi import _cost_model
        _cost_model.update(state["roi_cost_model"])

    if "token_broker_config" in state and state["token_broker_config"]:
        from control_plane.routers.token_broker import _config as _tb_config
        _tb_config.update(state["token_broker_config"])

    # Seed demo data so the dashboard isn't empty (skip in clean-install mode).
    # Traces + experiments are in-memory; metering usage records are DB-backed.
    # All seeders are idempotent (no-op when data already exists).
    import os
    if os.environ.get("OSTIARI_NO_DEMO", "").lower() not in ("1", "true", "yes"):
        from control_plane.demo_seed import (
            seed_demo_db,
            seed_demo_experiments,
            seed_demo_pricing,
        )
        from control_plane.routers.traces import seed_traces
        seed_traces()
        seed_demo_experiments()
        seed_demo_pricing()
        from control_plane.database import async_session
        async with async_session() as db:
            await seed_demo_db(db)

    # Start background health-check loop for gateways
    start_health_check()

    yield

    # Stop health-check loop
    stop_health_check()

    # Save in-memory state before shutdown
    from control_plane.routers.experiments import _experiments
    from control_plane.routers.model_config import _models
    from control_plane.routers.providers import _providers
    from control_plane.routers.quotas import _quotas
    from control_plane.routers.roi import _cost_model
    from control_plane.routers.token_broker import _config as _tb_config

    save_state({
        "quotas": [q.model_dump() for q in _quotas.values()],
        "experiments": [e.model_dump() for e in _experiments.values()],
        "models": [m.model_dump() for m in _models.values()],
        "providers": [p.model_dump() for p in _providers.values()],
        "roi_cost_model": _cost_model,
        "token_broker_config": _tb_config,
    })

    await engine.dispose()


app = FastAPI(
    title="Ostiari Control Plane",
    description="Centralized management for Ostiari gateway fleet",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(sso_router)
app.include_router(gateways.router)
app.include_router(agents.router)
app.include_router(tools.router)
app.include_router(policies.router)
app.include_router(mcp_servers.router)
app.include_router(costs.router)
app.include_router(discovery.router)
app.include_router(experiments.router)
app.include_router(model_config.router)
app.include_router(providers.router)
app.include_router(quotas.router)
app.include_router(proxy.router)
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
