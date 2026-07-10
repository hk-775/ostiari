"""Data models for the A2A (Agent-to-Agent) protocol."""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskState(str, Enum):
    submitted = "submitted"
    working = "working"
    input_required = "input-required"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class FilePart(BaseModel):
    type: Literal["file"] = "file"
    file: dict[str, Any] = Field(default_factory=dict)


class DataPart(BaseModel):
    type: Literal["data"] = "data"
    data: dict[str, Any] = Field(default_factory=dict)


Part = TextPart | FilePart | DataPart


class Message(BaseModel):
    role: Literal["user", "agent"]
    parts: list[Part]
    metadata: dict[str, Any] = Field(default_factory=dict)


class Artifact(BaseModel):
    name: str = ""
    description: str = ""
    parts: list[Part] = Field(default_factory=list)
    index: int = 0
    append: bool = False
    last_chunk: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class PushNotificationConfig(BaseModel):
    url: str
    token: str = ""
    authentication: dict[str, Any] = Field(default_factory=dict)


class TaskSendParams(BaseModel):
    id: str
    message: Message
    session_id: str = ""
    history_length: int | None = None
    push_notification: PushNotificationConfig | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskQueryParams(BaseModel):
    id: str
    history_length: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Task(BaseModel):
    id: str
    session_id: str = ""
    status: "TaskStatus"
    history: list[Message] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskStatus(BaseModel):
    state: TaskState
    message: Message | None = None
    timestamp: str = ""


Task.model_rebuild()


class AgentSkill(BaseModel):
    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    input_modes: list[str] = Field(default_factory=lambda: ["text"])
    output_modes: list[str] = Field(default_factory=lambda: ["text"])


class AgentAuthentication(BaseModel):
    schemes: list[str] = Field(default_factory=list)
    credentials: str = ""


class AgentCapabilities(BaseModel):
    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = False


class AgentCard(BaseModel):
    name: str
    description: str = ""
    url: str
    version: str = "1.0"
    skills: list[AgentSkill] = Field(default_factory=list)
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    authentication: AgentAuthentication | None = None
    default_input_modes: list[str] = Field(default_factory=lambda: ["text"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text"])
    provider: dict[str, str] = Field(default_factory=dict)


class A2AAgentConfig(BaseModel):
    """Configuration for connecting to a remote A2A agent."""

    name: str
    url: str
    auth_token: str = ""
    timeout_seconds: float = 30.0
    streaming: bool = True
