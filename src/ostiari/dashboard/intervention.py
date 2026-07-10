"""Intervention broker — distributed coordination for human decisions."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger("ostiari.dashboard")

REQUEST_CHANNEL = "ostiari:intervention:request"
RESPONSE_CHANNEL = "ostiari:intervention:response"


class InterventionBroker:
    """Coordinates intervention requests between Guard, TUI, and Dashboard."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def request_intervention(
        self,
        action: str,
        risk_score: float,
        question: str,
        timeout: float = 60.0,
    ) -> bool:
        request_id = str(uuid.uuid4())
        await self._redis.publish(
            REQUEST_CHANNEL,
            json.dumps(
                {
                    "request_id": request_id,
                    "action": action,
                    "risk_score": risk_score,
                    "question": question,
                    "timeout": timeout,
                }
            ),
        )

        pubsub = self._redis.pubsub()
        await pubsub.subscribe(RESPONSE_CHANNEL)
        deadline = time.monotonic() + timeout
        try:
            async for message in pubsub.listen():
                if time.monotonic() > deadline:
                    return False
                if message["type"] != "message":
                    continue
                data = json.loads(message["data"])
                if data.get("request_id") == request_id:
                    return data.get("approved", False)
        finally:
            await pubsub.unsubscribe(RESPONSE_CHANNEL)
            await pubsub.close()

        return False

    async def respond(self, request_id: str, approved: bool) -> bool:
        lock_key = f"ostiari:intervention:lock:{request_id}"
        acquired = await self._redis.set(lock_key, "1", nx=True, ex=120)
        if not acquired:
            return False
        await self._redis.publish(
            RESPONSE_CHANNEL,
            json.dumps({"request_id": request_id, "approved": approved}),
        )
        return True
