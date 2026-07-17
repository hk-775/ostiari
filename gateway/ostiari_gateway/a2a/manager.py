"""A2A Manager — discovers, connects, and manages remote A2A agent lifecycle."""

import logging
import uuid
from typing import Any

from ostiari_gateway.a2a.client import A2AClient
from ostiari_gateway.a2a.discovery import fetch_agent_card
from ostiari_gateway.a2a.models import (
    A2AAgentConfig,
    AgentCard,
    Message,
    Part,
    Task,
    TaskSendParams,
    TextPart,
)
from ostiari_gateway.a2a.protocol import JSONRPCError

log = logging.getLogger("ostiari.gateway.a2a")


class A2AManager:
    """Manages connections to remote A2A agents and exposes their skills as tools."""

    def __init__(self) -> None:
        self._clients: dict[str, A2AClient] = {}
        self._configs: dict[str, A2AAgentConfig] = {}
        self._cards: dict[str, AgentCard] = {}
        self._skill_to_agent: dict[str, str] = {}  # qualified_skill_id -> agent_name

    async def add_agent(self, config: A2AAgentConfig) -> dict[str, Any]:
        """Add a remote A2A agent: discover its card and register skills."""
        if config.name in self._clients:
            await self.remove_agent(config.name)

        try:
            card = await fetch_agent_card(
                config.url,
                timeout=config.timeout_seconds,
                auth_token=config.auth_token,
            )
        except Exception as e:
            log.error("Failed to discover A2A agent '%s' at %s: %s", config.name, config.url, e)
            return {"agent": config.name, "status": "error", "error": str(e)}

        # The discovery URL is the base; the card advertises the JSON-RPC endpoint
        # (often <base>/a2a). Point the client at the card's URL when it differs.
        if card.url and card.url.rstrip("/") != config.url.rstrip("/"):
            config = config.model_copy(update={"url": card.url})

        client = A2AClient(config)
        self._clients[config.name] = client
        self._configs[config.name] = config
        self._cards[config.name] = card

        for skill in card.skills:
            qualified = f"{config.name}.{skill.id}"
            self._skill_to_agent[qualified] = config.name

        return {
            "agent": config.name,
            "status": "connected",
            "url": config.url,
            "skills_discovered": len(card.skills),
            "skills": [f"{config.name}.{s.id}" for s in card.skills],
        }

    async def remove_agent(self, name: str) -> bool:
        """Disconnect and remove an A2A agent and its skills."""
        client = self._clients.pop(name, None)
        self._configs.pop(name, None)
        self._cards.pop(name, None)

        skills_to_remove = [sk for sk, ag in self._skill_to_agent.items() if ag == name]
        for sk in skills_to_remove:
            del self._skill_to_agent[sk]

        if client is not None:
            await client.close()
            log.info("Removed A2A agent '%s' (%d skills removed)", name, len(skills_to_remove))
            return True
        return False

    def list_agents(self) -> list[dict[str, Any]]:
        """List all connected A2A agents."""
        agents = []
        for name, config in self._configs.items():
            card = self._cards.get(name)
            skill_count = sum(1 for ag in self._skill_to_agent.values() if ag == name)
            agents.append({
                "name": name,
                "url": config.url,
                "connected": name in self._clients,
                "skills_count": skill_count,
                "description": card.description if card else "",
            })
        return agents

    def has_agent(self, name: str) -> bool:
        """Check if an agent with the given name is connected."""
        return name in self._clients

    def has_skill(self, qualified_name: str) -> bool:
        """Check if a qualified skill name is registered."""
        return qualified_name in self._skill_to_agent

    def get_agent_card(self, name: str) -> AgentCard | None:
        """Get the agent card for a connected agent."""
        return self._cards.get(name)

    def list_skills(self) -> list[dict[str, Any]]:
        """List all skills across all connected agents (as tool definitions)."""
        skills = []
        for name, card in self._cards.items():
            for skill in card.skills:
                qualified = f"{name}.{skill.id}"
                skills.append({
                    "name": qualified,
                    "description": skill.description,
                    "agent": name,
                    "tags": skill.tags,
                    "input_modes": skill.input_modes,
                    "output_modes": skill.output_modes,
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": f"Message to send to the '{skill.name}' skill",
                            },
                        },
                        "required": ["message"],
                    },
                })
        return skills

    async def call_agent(
        self, name: str, message: str, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Send a text message to an agent and return the result.

        ``headers`` carries delegation provenance (caller identity + chain) to
        the downstream agent's gateway so it can govern the interaction with
        full knowledge of who originated the request.
        """
        client = self._clients.get(name)
        if client is None:
            return {"error": f"A2A agent '{name}' not connected"}

        task_params = TaskSendParams(
            id=str(uuid.uuid4()),
            message=Message(
                role="user",
                parts=[TextPart(text=message)],
            ),
        )

        result = await client.send_task(task_params, headers=headers)

        if isinstance(result, JSONRPCError):
            return {"error": result.message, "code": result.code}

        return self._task_to_response(result)

    async def call_skill(
        self, qualified_name: str, message: str, headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """Call a specific skill by its qualified name (agent.skill_id)."""
        agent_name = self._skill_to_agent.get(qualified_name)
        if agent_name is None:
            return {"error": f"A2A skill '{qualified_name}' not found"}

        return await self.call_agent(agent_name, message, headers=headers)

    async def shutdown(self) -> None:
        """Close all A2A agent connections."""
        for name in list(self._clients.keys()):
            await self.remove_agent(name)

    def _task_to_response(self, task: Task) -> dict[str, Any]:
        """Convert a Task into a simplified response dict."""
        response: dict[str, Any] = {
            "task_id": task.id,
            "state": task.status.state.value,
        }

        if task.status.message:
            response["status_message"] = self._extract_text(task.status.message.parts)

        if task.artifacts:
            response["artifacts"] = [
                {
                    "name": a.name,
                    "parts": [p.model_dump() for p in a.parts],
                }
                for a in task.artifacts
            ]

        if task.history:
            last_agent_msg = next(
                (m for m in reversed(task.history) if m.role == "agent"), None
            )
            if last_agent_msg:
                response["response"] = self._extract_text(last_agent_msg.parts)

        return response

    def _extract_text(self, parts: list[Part]) -> str:
        """Extract text content from message parts."""
        texts = [p.text for p in parts if hasattr(p, "text")]
        return "\n".join(texts)
