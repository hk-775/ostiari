"""Session manager for conversation persistence via AgentCore Memory."""

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from src.gateway.models import ChatCompletionRequest, ChatCompletionResponse


@runtime_checkable
class MemoryClient(Protocol):
    """Protocol for the AgentCore Memory interface."""

    async def get_events(self, session_id: str) -> list[dict]:
        """Retrieve conversation events from STM."""
        ...

    async def store_event(self, session_id: str, event: dict) -> None:
        """Store a single event in STM."""
        ...

    async def store_knowledge(self, session_id: str, facts: list[str]) -> None:
        """Store facts in LTM."""
        ...


class SessionManager:
    """Manages conversation persistence via AgentCore Memory."""

    def __init__(self, memory_client: MemoryClient):
        self.memory = memory_client

    async def get_conversation_history(self, session_id: str) -> list[dict]:
        """Retrieve conversation events from AgentCore Memory STM."""
        return await self.memory.get_events(session_id)

    async def store_exchange(
        self,
        session_id: str,
        request: ChatCompletionRequest,
        response: ChatCompletionResponse,
    ) -> None:
        """Store request/response as events in AgentCore Memory STM."""
        timestamp = datetime.now(timezone.utc).isoformat()

        request_event = {
            "type": "request",
            "messages": request.messages,
            "model": request.model,
            "timestamp": timestamp,
        }
        await self.memory.store_event(session_id, request_event)

        response_event = {
            "type": "response",
            "choices": response.choices,
            "model": response.model,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
            "timestamp": timestamp,
        }
        await self.memory.store_event(session_id, response_event)

    async def store_semantic_knowledge(
        self, session_id: str, facts: list[str]
    ) -> None:
        """Store extracted facts in AgentCore Memory LTM."""
        await self.memory.store_knowledge(session_id, facts)
