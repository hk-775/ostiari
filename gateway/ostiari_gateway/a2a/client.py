"""A2A client — sends tasks to remote A2A agents via JSON-RPC over HTTP."""

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ostiari_gateway.a2a.models import (
    A2AAgentConfig,
    Message,
    Task,
    TaskQueryParams,
    TaskSendParams,
)
from ostiari_gateway.a2a.protocol import (
    ERROR_INTERNAL,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
    SSEEvent,
)

log = logging.getLogger("ostiari.gateway.a2a.client")


class A2AClient:
    """Client for communicating with a remote A2A agent."""

    def __init__(self, config: A2AAgentConfig) -> None:
        self._config = config
        self._base_url = config.url.rstrip("/")
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.auth_token:
            headers["Authorization"] = f"Bearer {config.auth_token}"
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=config.timeout_seconds,
        )

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def url(self) -> str:
        return self._base_url

    async def send_task(self, params: TaskSendParams) -> Task | JSONRPCError:
        return await self._call_task_method("tasks/send", params.model_dump(exclude_none=True))

    async def get_task(self, params: TaskQueryParams) -> Task | JSONRPCError:
        return await self._call_task_method("tasks/get", params.model_dump(exclude_none=True))

    async def cancel_task(self, task_id: str) -> Task | JSONRPCError:
        return await self._call_task_method("tasks/cancel", {"id": task_id})

    async def send_message(self, task_id: str, message: Message) -> Task | JSONRPCError:
        """Send a follow-up message for multi-turn conversations."""
        params = TaskSendParams(id=task_id, message=message)
        return await self.send_task(params)

    async def send_task_streaming(
        self, params: TaskSendParams
    ) -> AsyncIterator[SSEEvent]:
        """Send a task and stream SSE events for long-running operations."""
        request = JSONRPCRequest(
            id=str(uuid.uuid4()),
            method="tasks/sendSubscribe",
            params=params.model_dump(exclude_none=True),
        )

        async with self._client.stream(
            "POST",
            self._base_url,
            json=request.model_dump(),
        ) as response:
            response.raise_for_status()
            current_event = ""
            current_data: list[str] = []
            current_id = ""

            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    current_data.append(line[5:].strip())
                elif line.startswith("id:"):
                    current_id = line[3:].strip()
                elif line == "":
                    if current_data:
                        yield SSEEvent(
                            event=current_event or "message",
                            data="\n".join(current_data),
                            id=current_id,
                        )
                    current_event = ""
                    current_data = []
                    current_id = ""

            if current_data:
                yield SSEEvent(
                    event=current_event or "message",
                    data="\n".join(current_data),
                    id=current_id,
                )

    async def _call_task_method(
        self, method: str, params: dict[str, Any]
    ) -> Task | JSONRPCError:
        request = JSONRPCRequest(
            id=str(uuid.uuid4()),
            method=method,
            params=params,
        )

        try:
            response = await self._client.post(
                self._base_url,
                json=request.model_dump(),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.error("A2A request to %s failed: %s", self._base_url, e)
            return JSONRPCError(code=e.response.status_code, message=str(e))
        except httpx.RequestError as e:
            log.error("A2A connection to %s failed: %s", self._base_url, e)
            return JSONRPCError(code=ERROR_INTERNAL, message=str(e))

        rpc_response = JSONRPCResponse(**response.json())
        if rpc_response.error:
            return rpc_response.error

        return Task(**rpc_response.result)

    async def close(self) -> None:
        await self._client.aclose()
