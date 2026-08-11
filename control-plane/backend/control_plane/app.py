"""FastAPI application for the control plane."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from control_plane.auth.middleware import AuthMiddleware
from control_plane.auth.router import router as auth_router
from control_plane.auth.sso_router import router as sso_router
from control_plane.database import engine
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
    providers,
    proxy,
    quotas,
    roi,
    routing_controls,
    token_broker,
    tools,
    traces,
    trust,
)
from ostiari.http_limits import BodySizeLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    import control_plane.auth.models  # noqa: F401 — register auth tables
    from control_plane.persistence import load_state, save_state
    from control_plane.routers.gateways import start_health_check, stop_health_check

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Restore in-memory state from previous session. Records are tagged with an
    # "_org" key so per-org stores rebuild correctly; state files written before
    # multi-tenancy have no "_org" and fall back to the default org.
    from control_plane.models.database import DEFAULT_ORG
    state = load_state()

    def _org_of(rec: dict) -> str:
        return rec.get("_org") or DEFAULT_ORG

    if "quotas" in state:
        from control_plane.routers.quotas import QuotaResponse, _next_id, _quotas
        for q in state["quotas"]:
            org = _org_of(q)
            data = {k: v for k, v in q.items() if k != "_org"}
            _quotas[org][data["id"]] = QuotaResponse(**data)
        for org, quotas in _quotas.items():
            if quotas:
                _next_id[org] = max(quotas) + 1

    if "budget_alerts" in state:
        from control_plane.routers.quotas import BudgetAlert, _alerts
        for a in state["budget_alerts"]:
            org = _org_of(a)
            data = {k: v for k, v in a.items() if k != "_org"}
            # append, not assign: the deque's maxlen is what bounds this store, and
            # replacing it with a plain list would quietly remove that bound.
            _alerts[org].append(BudgetAlert(**data))

    if "experiments" in state:
        from control_plane.routers.experiments import ExperimentResponse, _experiments
        for e in state["experiments"]:
            org = _org_of(e)
            data = {k: v for k, v in e.items() if k != "_org"}
            _experiments[org][data["name"]] = ExperimentResponse(**data)

    if "models" in state:
        from control_plane.routers.model_config import ModelConfig, _models
        for m in state["models"]:
            org = _org_of(m)
            data = {k: v for k, v in m.items() if k != "_org"}
            _models[org][data["name"]] = ModelConfig(**data)

    if "agent_routing" in state:
        from control_plane.routers.agent_routing import RoutingPolicy, _policies
        for item in state["agent_routing"]:
            org = _org_of(item)
            data = {k: v for k, v in item.items() if k != "_org"}
            policy = RoutingPolicy(**data)
            _policies[(org, policy.gateway_id, policy.agent_id)] = policy

    if "providers" in state:
        from control_plane.routers.providers import _ProviderRecord, _providers
        for p in state["providers"]:
            org = _org_of(p)
            data = {k: v for k, v in p.items() if k != "_org"}
            _providers[org][data["name"]] = _ProviderRecord(**data)

    if "roi_cost_model" in state and state["roi_cost_model"]:
        from control_plane.routers.roi import _cost_model
        cm = state["roi_cost_model"]
        # New shape: {org: {...}}. Old flat shape {"entries":..,"fallback":..} → default org.
        if "entries" in cm or "fallback" in cm:
            _cost_model[DEFAULT_ORG].update(cm)
        else:
            for org, sub in cm.items():
                _cost_model[org].update(sub)

    if "token_broker_config" in state and state["token_broker_config"]:
        from control_plane.routers.token_broker import _config as _tb_config
        cfg = state["token_broker_config"]
        # New shape: {org: {...}}. Old flat shape (has bulk_discount/markup) → default org.
        if "bulk_discount" in cfg or "markup" in cfg:
            _tb_config[DEFAULT_ORG].update(cfg)
        else:
            for org, sub in cfg.items():
                _tb_config[org].update(sub)

    # Seed demo data so the dashboard isn't empty (skip in clean-install mode).
    # Traces + experiments are in-memory; metering usage records are DB-backed.
    # All seeders are idempotent (no-op when data already exists).
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
        from control_plane.routers.traces import seed_traces
        seed_traces()
        seed_demo_agents()
        seed_demo_approvals()
        seed_demo_experiments()
        seed_demo_pricing()
        from control_plane.database import async_session
        async with async_session() as db:
            await seed_demo_db(db)
            # Both of these read the usage records seed_demo_db writes — quota
            # spend and broker pool draw-down are aggregates over them — so on a
            # first run they have to exist first.
            await seed_demo_quotas(db)
            await seed_demo_broker_pools(db)

    # Start background health-check loop for gateways
    start_health_check()

    yield

    # Stop health-check loop
    stop_health_check()

    # Save in-memory state before shutdown
    from control_plane.routers.agent_routing import _policies
    from control_plane.routers.experiments import _experiments
    from control_plane.routers.model_config import _models
    from control_plane.routers.providers import _providers
    from control_plane.routers.quotas import _alerts, _quotas
    from control_plane.routers.roi import _cost_model
    from control_plane.routers.token_broker import _config as _tb_config

    # Flatten the org-nested stores into tagged record lists ({..., "_org": org})
    # so restore can rebuild the per-org structure. cost_model/config are already
    # org-keyed dicts and serialize directly.
    def _dump(store) -> list:
        out = []
        for org, inner in store.items():
            for rec in inner.values():
                out.append({**rec.model_dump(), "_org": org})
        return out

    # Same tagging, for the org-keyed stores holding sequences rather than dicts.
    def _dump_seq(store) -> list:
        return [
            {**rec.model_dump(), "_org": org}
            for org, seq in store.items()
            for rec in seq
        ]

    save_state({
        "quotas": _dump(_quotas),
        # Budget alerts were the one in-memory store that wasn't saved, so a
        # restart silently discarded them — an operator could miss that a gateway
        # blew through 100% of its budget purely because the control plane
        # bounced. Bounded by the deque's maxlen, so this stays small.
        "budget_alerts": _dump_seq(_alerts),
        "experiments": _dump(_experiments),
        "models": _dump(_models),
        "agent_routing": [
            {**policy.model_dump(), "_org": org}
            for (org, _gateway_id, _agent_id), policy in _policies.items()
        ],
        "providers": _dump(_providers),
        "roi_cost_model": dict(_cost_model),
        "token_broker_config": dict(_tb_config),
    })

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
