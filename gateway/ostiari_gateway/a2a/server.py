"""A2A server — exposes this gateway as an A2A agent via FastAPI routes."""

import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ostiari_gateway.a2a.models import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Message,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from ostiari_gateway.a2a.protocol import (
    ERROR_INTERNAL,
    ERROR_INVALID_PARAMS,
    ERROR_INVALID_REQUEST,
    ERROR_METHOD_NOT_FOUND,
    ERROR_TASK_NOT_CANCELABLE,
    ERROR_TASK_NOT_FOUND,
    JSONRPCError,
    JSONRPCRequest,
    JSONRPCResponse,
)

log = logging.getLogger("ostiari.gateway.a2a.server")


class A2AServer:
    """Handles incoming A2A requests, exposing gateway tools as agent skills."""

    def __init__(
        self,
        gateway_name: str = "Ostiari Gateway",
        gateway_description: str = "Agent safety and reliability gateway",
        gateway_url: str = "",
    ) -> None:
        self._name = gateway_name
        self._description = gateway_description
        self._url = gateway_url
        self._tasks: dict[str, Task] = {}
        self._skills: list[AgentSkill] = []
        self._task_handler: Any = None

    def set_skills(self, skills: list[AgentSkill]) -> None:
        """Update the skills advertised in the agent card."""
        self._skills = skills

    def set_task_handler(self, handler: Any) -> None:
        """Set the async callable that processes incoming tasks.

        Handler signature: async def handler(task_params: dict) -> Task
        """
        self._task_handler = handler

    def get_agent_card(self) -> AgentCard:
        return AgentCard(
            name=self._name,
            description=self._description,
            url=self._url,
            skills=self._skills,
            capabilities=AgentCapabilities(
                streaming=True,
                push_notifications=False,
                state_transition_history=True,
            ),
        )

    def create_router(self) -> APIRouter:
        """Create FastAPI router with A2A endpoints."""
        router = APIRouter()

        @router.get("/.well-known/agent.json")
        async def agent_card() -> Any:
            return self.get_agent_card().model_dump(exclude_none=True)

        @router.post("/a2a")
        async def handle_a2a(request: Request) -> Any:
            try:
                body = await request.json()
            except Exception:
                return self._error_response(
                    None, ERROR_INVALID_REQUEST, "Invalid JSON"
                )

            try:
                rpc_request = JSONRPCRequest(**body)
            except Exception as e:
                return self._error_response(
                    body.get("id"), ERROR_INVALID_REQUEST, f"Invalid JSON-RPC request: {e}"
                )

            method = rpc_request.method

            if method == "tasks/send":
                return await self._handle_send(rpc_request)
            elif method == "tasks/get":
                return await self._handle_get(rpc_request)
            elif method == "tasks/cancel":
                return await self._handle_cancel(rpc_request)
            elif method == "tasks/sendSubscribe":
                return await self._handle_send_subscribe(rpc_request)
            else:
                return self._error_response(
                    rpc_request.id, ERROR_METHOD_NOT_FOUND, f"Unknown method: {method}"
                )

        return router

    async def _handle_send(self, request: JSONRPCRequest) -> JSONResponse:
        task_id = request.params.get("id", str(uuid.uuid4()))
        message_data = request.params.get("message")

        if not message_data:
            return self._error_response(
                request.id, ERROR_INVALID_PARAMS, "Missing 'message' in params"
            )

        task = self._get_or_create_task(task_id, request.params)

        if self._task_handler:
            try:
                result_task = await self._task_handler(request.params)
                self._tasks[task_id] = result_task
                task = result_task
            except Exception as e:
                log.error("Task handler failed for task %s: %s", task_id, e)
                task.status = TaskStatus(state=TaskState.failed)
                self._tasks[task_id] = task
                return self._error_response(
                    request.id, ERROR_INTERNAL, f"Task processing failed: {e}"
                )
        else:
            task.status = TaskStatus(
                state=TaskState.completed,
                message=Message(
                    role="agent",
                    parts=[TextPart(text="No task handler configured")],
                ),
            )
            self._tasks[task_id] = task

        rpc_response = JSONRPCResponse(
            id=request.id,
            result=task.model_dump(exclude_none=True),
        )
        return JSONResponse(content=rpc_response.model_dump(exclude_none=True))

    async def _handle_get(self, request: JSONRPCRequest) -> JSONResponse:
        task_id = request.params.get("id")
        if not task_id:
            return self._error_response(
                request.id, ERROR_INVALID_PARAMS, "Missing 'id' in params"
            )

        task = self._tasks.get(task_id)
        if not task:
            return self._error_response(
                request.id, ERROR_TASK_NOT_FOUND, f"Task not found: {task_id}"
            )

        history_length = request.params.get("history_length")
        if history_length is not None and task.history:
            task_copy = task.model_copy()
            task_copy.history = task_copy.history[-history_length:]
            task = task_copy

        rpc_response = JSONRPCResponse(
            id=request.id,
            result=task.model_dump(exclude_none=True),
        )
        return JSONResponse(content=rpc_response.model_dump(exclude_none=True))

    async def _handle_cancel(self, request: JSONRPCRequest) -> JSONResponse:
        task_id = request.params.get("id")
        if not task_id:
            return self._error_response(
                request.id, ERROR_INVALID_PARAMS, "Missing 'id' in params"
            )

        task = self._tasks.get(task_id)
        if not task:
            return self._error_response(
                request.id, ERROR_TASK_NOT_FOUND, f"Task not found: {task_id}"
            )

        cancelable_states = {TaskState.submitted, TaskState.working, TaskState.input_required}
        if task.status.state not in cancelable_states:
            return self._error_response(
                request.id, ERROR_TASK_NOT_CANCELABLE,
                f"Task in state '{task.status.state.value}' cannot be canceled",
            )

        task.status = TaskStatus(state=TaskState.canceled)
        rpc_response = JSONRPCResponse(
            id=request.id,
            result=task.model_dump(exclude_none=True),
        )
        return JSONResponse(content=rpc_response.model_dump(exclude_none=True))

    async def _handle_send_subscribe(self, request: JSONRPCRequest) -> StreamingResponse:
        """Handle streaming task execution via SSE."""
        task_id = request.params.get("id", str(uuid.uuid4()))
        message_data = request.params.get("message")

        if not message_data:
            error_resp = self._error_response(
                request.id, ERROR_INVALID_PARAMS, "Missing 'message' in params"
            )
            return error_resp  # type: ignore[return-value]

        task = self._get_or_create_task(task_id, request.params)

        async def event_stream() -> AsyncGenerator[str, None]:
            task.status = TaskStatus(state=TaskState.working)
            self._tasks[task_id] = task
            yield self._sse_event("status", task.model_dump(exclude_none=True))

            if self._task_handler:
                try:
                    result_task = await self._task_handler(request.params)
                    self._tasks[task_id] = result_task
                    yield self._sse_event("status", result_task.model_dump(exclude_none=True))
                    if result_task.artifacts:
                        for artifact in result_task.artifacts:
                            yield self._sse_event(
                                "artifact", artifact.model_dump(exclude_none=True)
                            )
                except Exception as e:
                    log.error("Streaming task handler failed: %s", e)
                    task.status = TaskStatus(state=TaskState.failed)
                    self._tasks[task_id] = task
                    yield self._sse_event("status", task.model_dump(exclude_none=True))
            else:
                task.status = TaskStatus(
                    state=TaskState.completed,
                    message=Message(
                        role="agent",
                        parts=[TextPart(text="No task handler configured")],
                    ),
                )
                self._tasks[task_id] = task
                yield self._sse_event("status", task.model_dump(exclude_none=True))

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    def _get_or_create_task(self, task_id: str, params: dict[str, Any]) -> Task:
        """Get existing task or create a new one from params."""
        existing = self._tasks.get(task_id)
        if existing:
            message_data = params.get("message")
            if message_data:
                msg = Message(**message_data)
                existing.history.append(msg)
            return existing

        message_data = params.get("message", {})
        msg = Message(**message_data) if message_data else Message(
            role="user", parts=[TextPart(text="")]
        )

        task = Task(
            id=task_id,
            session_id=params.get("session_id", ""),
            status=TaskStatus(state=TaskState.submitted),
            history=[msg],
        )
        self._tasks[task_id] = task
        return task

    def _error_response(
        self, request_id: Any, code: int, message: str
    ) -> JSONResponse:
        rpc_response = JSONRPCResponse(
            id=request_id or 0,
            error=JSONRPCError(code=code, message=message),
        )
        return JSONResponse(
            content=rpc_response.model_dump(exclude_none=True),
            status_code=200,  # JSON-RPC errors still use HTTP 200
        )

    def _sse_event(self, event: str, data: Any) -> str:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n"
