"""FastAPI application for the control plane."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from control_plane.database import engine
from control_plane.models.database import Base
from control_plane.routers import agents, audit, costs, experiments, mcp_servers, model_config, policies, proxy, quotas, gateways, tools, traces


@asynccontextmanager
async def lifespan(app: FastAPI):
    from control_plane.persistence import load_state, save_state
    from control_plane.routers.gateways import start_health_check, stop_health_check

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Restore in-memory state from previous session
    state = load_state()
    if "quotas" in state:
        from control_plane.routers.quotas import _quotas, _next_id, QuotaResponse
        for q in state["quotas"]:
            _quotas[q["id"]] = QuotaResponse(**q)
        if state["quotas"]:
            _next_id[0] = max(q["id"] for q in state["quotas"]) + 1

    if "experiments" in state:
        from control_plane.routers.experiments import _experiments, ExperimentResponse
        for e in state["experiments"]:
            _experiments[e["name"]] = ExperimentResponse(**e)

    if "models" in state:
        from control_plane.routers.model_config import _models, ModelConfig
        for m in state["models"]:
            _models[m["name"]] = ModelConfig(**m)

    # Start background health-check loop for gateways
    start_health_check()

    yield

    # Stop health-check loop
    stop_health_check()

    # Save in-memory state before shutdown
    from control_plane.routers.quotas import _quotas
    from control_plane.routers.experiments import _experiments
    from control_plane.routers.model_config import _models

    save_state({
        "quotas": [q.model_dump() for q in _quotas.values()],
        "experiments": [e.model_dump() for e in _experiments.values()],
        "models": [m.model_dump() for m in _models.values()],
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

app.include_router(gateways.router)
app.include_router(agents.router)
app.include_router(tools.router)
app.include_router(policies.router)
app.include_router(mcp_servers.router)
app.include_router(costs.router)
app.include_router(experiments.router)
app.include_router(model_config.router)
app.include_router(quotas.router)
app.include_router(proxy.router)
app.include_router(traces.router)
app.include_router(audit.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "control-plane"}
