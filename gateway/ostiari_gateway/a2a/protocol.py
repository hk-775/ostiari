"""A2A JSON-RPC protocol types and SSE event parsing."""

from typing import Any

from pydantic import BaseModel, Field


class JSONRPCRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class JSONRPCResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: str | int
    result: Any = None
    error: "JSONRPCError | None" = None


class JSONRPCError(BaseModel):
    code: int
    message: str
    data: Any = None


JSONRPCResponse.model_rebuild()


class JSONRPCNotification(BaseModel):
    jsonrpc: str = "2.0"
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


A2A_METHODS = {
    "tasks/send": "Send a task to the agent",
    "tasks/get": "Get task status and history",
    "tasks/cancel": "Cancel a running task",
    "tasks/sendSubscribe": "Send a task and subscribe to SSE updates",
    "tasks/pushNotification/set": "Configure push notifications for a task",
    "tasks/pushNotification/get": "Get push notification config for a task",
}

ERROR_TASK_NOT_FOUND = -32001
ERROR_TASK_NOT_CANCELABLE = -32002
ERROR_PUSH_NOT_SUPPORTED = -32003
ERROR_UNSUPPORTED_OPERATION = -32004
ERROR_PARSE_ERROR = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602
ERROR_INTERNAL = -32603


class SSEEvent:
    """Parsed Server-Sent Event."""

    def __init__(self, event: str = "message", data: str = "", id: str = "") -> None:
        self.event = event
        self.data = data
        self.id = id


def parse_sse_stream(raw: str) -> list[SSEEvent]:
    """Parse a raw SSE text stream into a list of events."""
    events: list[SSEEvent] = []
    current_event = ""
    current_data: list[str] = []
    current_id = ""

    for line in raw.split("\n"):
        if line.startswith("event:"):
            current_event = line[6:].strip()
        elif line.startswith("data:"):
            current_data.append(line[5:].strip())
        elif line.startswith("id:"):
            current_id = line[3:].strip()
        elif line == "":
            if current_data:
                events.append(SSEEvent(
                    event=current_event or "message",
                    data="\n".join(current_data),
                    id=current_id,
                ))
            current_event = ""
            current_data = []
            current_id = ""

    if current_data:
        events.append(SSEEvent(
            event=current_event or "message",
            data="\n".join(current_data),
            id=current_id,
        ))

    return events
