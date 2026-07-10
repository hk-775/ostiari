"""FastAPI application factory for the Ostiari dashboard."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from ostiari.dashboard.cache import QueryCache
from ostiari.dashboard.dependencies import init_dependencies
from ostiari.dashboard.intervention import InterventionBroker
from ostiari.dashboard.middleware import TokenAuthMiddleware
from ostiari.dashboard.poller import TracePoller
from ostiari.dashboard.routers import (
    agents,
    breakers,
    config,
    health,
    intervention,
    report,
    stats,
    traces,
)
from ostiari.dashboard.storage_async import AsyncStorageWrapper
from ostiari.dashboard.websocket import WebSocketManager
from ostiari.storage import SQLiteBackend

logger = logging.getLogger("ostiari.dashboard")

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    storage: Any | None = None,
    redis_url: str | None = None,
    token: str | None = None,
) -> FastAPI:
    """Create and configure the FastAPI dashboard application."""
    app = FastAPI(title="Ostiari Dashboard", version="0.1.0")

    redis_url = redis_url or os.environ.get("AGENTGUARD_REDIS_URL")
    token = token or os.environ.get("AGENTGUARD_TOKEN")

    raw_storage = storage or SQLiteBackend(path=os.environ.get("AGENTGUARD_DB", "ostiari.db"))
    async_storage = AsyncStorageWrapper(raw_storage)

    redis: Any = None
    if redis_url:
        try:
            from redis.asyncio import Redis

            redis = Redis.from_url(redis_url)
        except Exception as e:
            logger.warning("Redis unavailable: %s", e)

    cache = QueryCache(redis=redis)
    ws_manager = WebSocketManager(redis_url=redis_url)
    poller = TracePoller(storage=async_storage, ws_manager=ws_manager)
    intervention_broker = InterventionBroker(redis) if redis else None

    init_dependencies(
        storage=async_storage,
        cache=cache,
        intervention=intervention_broker,
        raw_storage=raw_storage,
    )

    if token:
        app.add_middleware(TokenAuthMiddleware, token=token)
        host = os.environ.get("AGENTGUARD_HOST", "127.0.0.1")
        if host != "127.0.0.1" and host != "localhost":
            logger.info("Token auth enabled for network-exposed dashboard")

    app.include_router(traces.router)
    app.include_router(breakers.router)
    app.include_router(stats.router)
    app.include_router(agents.router)
    app.include_router(report.router)
    app.include_router(health.router)
    app.include_router(config.router)
    app.include_router(intervention.router)

    @app.websocket("/ws/live")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            ws_manager.disconnect(websocket)

    @app.on_event("startup")
    async def startup() -> None:
        await ws_manager.startup()
        await poller.start()
        logger.info("Dashboard started")

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await poller.stop()
        await ws_manager.shutdown()
        raw_storage.close()
        logger.info("Dashboard stopped")

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app
