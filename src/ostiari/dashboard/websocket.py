"""WebSocket connection manager with Redis pub/sub fan-out."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger("ostiari.dashboard")

TRACES_CHANNEL = "ostiari:traces"
INTERVENTION_REQ_CHANNEL = "ostiari:intervention:request"
INTERVENTION_RES_CHANNEL = "ostiari:intervention:response"


class WebSocketManager:
    """Manages WebSocket connections with optional Redis pub/sub for multi-worker scaling."""

    def __init__(self, redis_url: str | None = None) -> None:
        self._connections: set[WebSocket] = set()
        self._redis: Any = None
        self._redis_url = redis_url
        self._pubsub: Any = None

    async def startup(self) -> None:
        if self._redis_url:
            try:
                from redis.asyncio import Redis

                self._redis = Redis.from_url(self._redis_url)
                self._pubsub = self._redis.pubsub()
                await self._pubsub.subscribe(
                    TRACES_CHANNEL,
                    INTERVENTION_REQ_CHANNEL,
                    INTERVENTION_RES_CHANNEL,
                )
                asyncio.create_task(self._listen_redis())
                logger.info("WebSocket manager connected to Redis")
            except Exception as e:
                logger.warning("Redis unavailable, operating in single-worker mode: %s", e)
                self._redis = None

    async def shutdown(self) -> None:
        if self._pubsub:
            await self._pubsub.unsubscribe()
            await self._pubsub.close()
        if self._redis:
            await self._redis.close()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def publish_traces(self, traces: list[dict[str, Any]]) -> None:
        if self._redis:
            payload = json.dumps(traces)
            await self._redis.publish(TRACES_CHANNEL, payload)
        else:
            messages = [{"type": "trace", "data": t} for t in traces]
            await self._broadcast(messages)

    async def _listen_redis(self) -> None:
        try:
            async for message in self._pubsub.listen():
                if message["type"] != "message":
                    continue
                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode()
                data = json.loads(message["data"])

                if channel == TRACES_CHANNEL:
                    messages = [{"type": "trace", "data": t} for t in data]
                elif channel == INTERVENTION_REQ_CHANNEL:
                    messages = [{"type": "intervention", "data": data}]
                elif channel == INTERVENTION_RES_CHANNEL:
                    messages = [{"type": "intervention_resolved", "data": data}]
                else:
                    continue

                await self._broadcast(messages)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Redis listener error: %s", e)

    async def _broadcast(self, messages: list[dict[str, Any]]) -> None:
        dead: set[WebSocket] = set()
        for ws in self._connections:
            try:
                for msg in messages:
                    await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._connections -= dead
